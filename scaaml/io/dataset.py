"Build and load tensorFlow dataset Record wrapper"
import math
import json
import os
import tensorflow as tf
from typing import Dict, List, Union
from pathlib import Path
from termcolor import cprint
from collections import defaultdict
from tqdm.auto import tqdm
from tabulate import tabulate
from scaaml.utils import bytelist_to_hex
from time import time
from .utils import sha256sum
from .shard import Shard


class Dataset():
    def __init__(
        self,
        root_path: str,
        architecture: str,
        implementation: str,
        algorithm: str,
        version: int,
        chip_id: int,
        firmware_sha256: str,
        purpose: str,
        comment: str,
        examples_per_shard: int,
        measurements_info: Dict,
        attack_points_info: Dict,
        compression: str = "GZIP",
        shards_list: defaultdict = None,
        keys_per_split: defaultdict = None,
        examples_per_split: defaultdict = None,
        capture_info: dict = {},
        min_values: Dict[str, int] = {},
        max_values: Dict[str, int] = {},
    ) -> None:
        self.root_path = root_path
        self.architecture = architecture
        self.implementation = implementation
        self.algorithm = algorithm
        self.version = version
        self.compression = compression
        self.chip_id = chip_id
        self.firmware_sha256 = firmware_sha256
        self.purpose = purpose
        self.comment = comment

        self.capture_info = capture_info
        self.measurements_info = measurements_info
        self.attack_points_info = attack_points_info

        if purpose not in ['train', 'holdout']:
            raise ValueError("Invalid purpose", purpose)

        if not self.firmware_sha256:
            raise ValueError("Firmware hash is required")

        # create directory -- check if its empty
        self.slug = "%s_%s_%s_v%s_%s" % (algorithm, architecture,
                                         implementation, version, purpose)
        self.path = Path(self.root_path) / self.slug
        if self.path.exists():
            cprint("[Warning] Path exist, some files might be over-written",
                   'yellow')
        else:
            # create path if needed
            self.path.mkdir(parents=True)
            Path(self.path / 'train').mkdir()
            Path(self.path / 'test').mkdir()
            Path(self.path / 'holdout').mkdir()

        cprint("Dataset path: %s" % self.path, 'green')

        # current shard tracking
        self.curr_shard_key = None  # current shard_key
        self.shard_key = None
        self.shard_path = None
        self.shard_split = None
        self.shard_part = None
        self.shard_relative_path = None  # for the shardlist
        self.curr_shard = None  # current_ shard object

        # counters - must be passed as param to allow reload.
        self.shards_list = shards_list or defaultdict(list)
        self.keys_per_split = keys_per_split or defaultdict(int)
        self.examples_per_split = examples_per_split or defaultdict(int)
        self.examples_per_shard = examples_per_shard
        self.min_values = min_values
        self.max_values = max_values
        for k in measurements_info.keys():
            # init only if not existing
            if k not in min_values:
                self.min_values[k] = math.inf
                self.max_values[k] = 0

        # write config
        self._write_config()

    def new_shard(self, key: list, part: int, split: str):
        """Initiate a new key

        Args:
            key: the key that was used to create the measurements.

            part: Indicate which part of a given key set of catpure this
            shard represent. Capture are splitted into parts to easily
            allow to restrict the number of traces used per key.

            split: the split the shard belongs to {train, test, holdout}
        """
        # finalize previous shard if need
        if self.curr_shard:
            self.close_shard()

        if split not in ['train', 'test', 'holdout']:
            raise ValueError("Invalid split, must be: {train, test, holdout}")

        if part < 1 or part > 10:
            raise ValueError("Invalid part value -- muse be in [1, 10]")

        self.shard_split = split
        self.shard_part = part
        self.shard_key = bytelist_to_hex(key, spacer='')

        # shard name
        fname = "%s_%s.tfrec" % (self.shard_key, self.shard_part)
        self.shard_relative_path = "%s/%s" % (split, fname)
        self.shard_path = str(self.path / self.shard_relative_path)

        # new shard
        self.curr_shard = Shard(self.shard_path,
                                attack_points_info=self.attack_points_info,
                                measurements_info=self.measurements_info,
                                compression=self.compression)

    def write_example(self, attack_points: Dict, measurement: Dict):
        self.curr_shard.write(attack_points, measurement)

    def close_shard(self):
        # close the shard

        stats = self.curr_shard.close()

        # update min/max values
        for k, v in stats['min_values'].items():
            self.min_values[k] = min(self.min_values[k], v)

        for k, v in stats['max_values'].items():
            self.max_values[k] = max(self.max_values[k], v)

        # update stats

        self.examples_per_split[self.shard_split] += stats['examples']
        print(self.shard_split)
        print(self.keys_per_split)
        self.keys_per_split[self.shard_split] += 1

        # record in shardlist
        self.shards_list[self.shard_split].append({
            "path": str(self.shard_relative_path),
            "examples": stats['examples'],
            "sha256": sha256sum(self.shard_path),
            "key": self.shard_key
        })

        # update config
        self._write_config()
        self.curr_shard = None

    @staticmethod
    def as_tfdataset(dataset_path: str,
                     split: str,
                     attack_points: Union[List, str],
                     traces: Union[List, str],
                     bytes: Union[List, int],
                     shards: int,
                     traces_max_len: int = None,
                     trace_block_size: int = 1,
                     batch_size: int = 32,
                     prefetch: int = 10,
                     file_parallelism: int = 1,
                     parallelism: int = os.cpu_count(),
                     shuffle: int = 1000
                     ) -> Union[tf.data.Dataset, Dict, Dict]:
        """"Dataset as tfdataset
        """

        trace_seq_len = traces_max_len // trace_block_size
        if traces_max_len % trace_block_size:
            raise ValueError("trace_max_len must be a multiple of len(traces)")

        # boxing
        if isinstance(traces, str):
            traces = [traces]
        if isinstance(bytes, int):
            bytes = [bytes]
        if isinstance(attack_points, str):
            attack_points = [attack_points]

        # loading info
        dpath = Path(dataset_path)
        dataset = Dataset.from_config(dataset_path)

        if split not in dataset.keys_per_split:
            raise ValueError("Unknown split -- see Dataset.summary() for list")

        # TF_FEATURES construction: must contains all features and be global
        tf_features = {}  # what is decoded
        for name, ipt in dataset.measurements_info.items():
            tf_features[name] = tf.io.FixedLenFeature([ipt['len']], tf.float32)
        for name, ap in dataset.attack_points_info.items():
            tf_features[name] = tf.io.FixedLenFeature([ap['len']], tf.int64)

        # decoding funtion
        def from_tfrecord(tfrecord):
            rec = tf.io.parse_single_example(tfrecord, tf_features)
            return rec

        # inputs construction
        inputs = {}  # model inputs
        for name in traces:
            ipt = dataset.measurements_info[name]
            inputs[name] = ipt

            inputs[name]['min'] = tf.constant(dataset.min_values[name])
            inputs[name]['max'] = tf.constant(dataset.max_values[name])
            inputs[name]['delta'] = tf.constant(inputs[name]['max'] - inputs[name]['min'])

        # output construction
        outputs = {}  # model outputs
        for name in attack_points:
            for b in bytes:
                n = "%s_%s" % (name, b)
                ap = dataset.attack_points_info[name]
                outputs[n] = ap
                outputs[n]['ap'] = name
                outputs[n]['byte'] = b

        # processing function
        # @tf.function
        def process_record(rec):
            "process the tf record to get it ready for learning"
            x = {}
            # normalize the traces
            for name, data in inputs.items():
                trace = rec[name]

                # truncate if needed
                if traces_max_len:
                    trace = trace[:traces_max_len]

                # rescale
                trace = 2 * ((trace - data['min']) / (data['delta'])) - 1

                # reshape
                trace = tf.reshape(trace, (trace_seq_len, trace_block_size))

                # assign
                x[name] = trace

            # one_hot the outptut for each ap/byte
            y = {}
            for name, data in outputs.items():
                v = tf.one_hot(rec[data['ap']][data['byte']], data['max_val'])
                y[name] = v

            return (x, y)

        # collect and truncate shard list of a given split
        # this is done prior to anything to allow to only download the nth
        # first shards
        shards_list = dataset.shards_list[split]
        if shards:
            shards_list = shards_list[:shards]
        shards_paths = [str(dpath / s['path']) for s in shards_list]
        num_shards = len(shards_paths)
        # print(shards_paths)
        # dataset creation
        # with tf.device('/cpu:0'):
        # shuffle the shard order
        ds = tf.data.Dataset.from_tensor_slices(shards_paths)

        # shuffle shard order
        ds = ds.shuffle(num_shards)
        # This is the tricky part, we are using the interleave function to
        # do the sampling as requested by the user. This is not the
        # standard use of the function or an obvious way to do it but
        # its by far the faster and more compatible way to do so
        # we are favoring for once those factors over readability
        # deterministic=False is not an error, it is what allows us to
        # create random batch
        ds = ds.interleave(
            lambda x: tf.data.TFRecordDataset(x,
                                            compression_type=dataset.compression),  # noqa
            cycle_length=num_shards,
            block_length=1,
            num_parallel_calls=file_parallelism,
            deterministic=False)
        # decode to records
        ds = ds.map(from_tfrecord, num_parallel_calls=parallelism)
        # process them
        ds = ds.map(process_record, num_parallel_calls=parallelism)

        # # randomize
        ds = ds.shuffle(shuffle)

        # # batching with repeat
        ds = ds.repeat()
        ds = ds.batch(batch_size)
        ds = ds.prefetch(prefetch)

        return ds, inputs, outputs

    @staticmethod
    def summary(dataset_path):
        """Print a summary of the dataset"""
        lst = [
            'architecture', 'implementation', 'algorithm', 'version',
            'chip_id', 'comment', 'purpose', 'compression'
        ]
        fpath = Dataset._get_config_path(dataset_path)
        config = json.loads(open(fpath).read())
        cprint("[Dataset Summary]", 'cyan')
        cprint("Info", 'yellow')
        print(tabulate([[k, config[k]] for k in lst]))

        cprint("\nAttack Points", 'yellow')
        d = [[k, v['len'], v['max_val']]
             for k, v in config['attack_points_info'].items()]
        print(tabulate(d, headers=['ap', 'len', 'max_val']))

        cprint("\nMeasurements", 'magenta')
        d = [[k, v['type'], v['len']]
             for k, v in config['measurements_info'].items()]
        print(tabulate(d, headers=['name', 'type', 'len']))

        cprint("\nContent", 'green')
        d = []
        for split in config['keys_per_split'].keys():
            d.append([
                split,
                config['keys_per_split'][split],
                config['examples_per_split'][split],
            ])
        print(tabulate(d, ['split', 'num_keys', 'num_examples']))

    @staticmethod
    def inspect(dataset_path, split, shard_id, num_example):
        """Display the content of a given shard"""
        fpath = Dataset._get_config_path(dataset_path)
        config = json.loads(open(fpath).read())
        spath = Path(fpath) / config['shards_list'][split][shard_id]['path']
        cprint("Reading shard %s" % spath, 'cyan')
        s = Shard(str(spath),
                  attack_points_info=config['attack_points_info'],
                  measurements_info=config['measurements_info'],
                  compression=config['compression'])
        data = s.read(num=num_example)
        print(data)
        return(data)

    def check(self):
        """Check the dataset integrity"""
        # check examples are balances
        seen_keys = {}  # use to ensure keys are not reused

        for split, expected_examples in self.examples_per_split.items():
            slist = self.shards_list[split]
            # checking we have the rigt number of shards
            if len(slist) != self.keys_per_split[split]:
                raise ValueError("Num shards in shard_list != self.shards")

            pb = tqdm(total=len(slist), desc="Checking %s split" % split)
            actual_examples = 0
            for sinfo in slist:

                # no key reuse
                if sinfo['key'] in seen_keys:
                    raise ValueError("Duplicate key", sinfo['key'])
                else:
                    seen_keys[sinfo['key']] = 1

                actual_examples += sinfo['examples']
                shard_path = self.path / sinfo['path']
                sh = sha256sum(shard_path)
                if sh != sinfo['sha256']:
                    raise ValueError(sinfo['path'], "SHA256 miss-match")
                pb.update()

            pb.close()

            if actual_examples != expected_examples:
                raise ValueError("sum example don't match top_examples")

    def _write_config(self):
        config = {
            "architecture": self.architecture,
            "implementation": self.implementation,
            "algorithm": self.algorithm,
            "version": self.version,
            "chip_id": self.chip_id,
            "firmware_sha256": self.firmware_sha256,
            "comment": self.comment,
            "purpose": self.purpose,
            "compression": self.compression,
            "shards_list": self.shards_list,
            "keys_per_split": self.keys_per_split,
            "examples_per_shard": self.examples_per_shard,
            "examples_per_split": self.examples_per_split,
            "capture_info": self.capture_info,
            "measurements_info": self.measurements_info,
            "attack_points_info": self.attack_points_info,
            "min_values": self.min_values,
            "max_values": self.max_values,
        }

        with open(self._get_config_path(self.path), 'w+') as o:
            o.write(json.dumps(config))

    @staticmethod
    def from_config(dataset_path: str):
        dpath = Path(dataset_path)
        fpath = Dataset._get_config_path(dataset_path)
        cprint("reloading %s" % fpath, 'magenta')
        config = json.loads(open(fpath).read())
        return Dataset(
            root_path=str(dpath),
            architecture=config['architecture'],
            implementation=config['implementation'],
            algorithm=config['algorithm'],
            version=config['version'],
            comment=config['comment'],
            purpose=config['purpose'],
            chip_id=config['chip_id'],
            firmware_sha256=config['firmware_sha256'],
            measurements_info=config['measurements_info'],
            attack_points_info=config['attack_points_info'],
            capture_info=config['capture_info'],
            compression=config['compression'],
            shards_list=config['shards_list'],
            keys_per_split=config['keys_per_split'],
            examples_per_split=config['examples_per_split'],
            examples_per_shard=config['examples_per_shard'],
            min_values=config['min_values'],
            max_values=config['max_values'],
        )

    @staticmethod
    def _get_config_path(path):
        return str(Path(path) / 'info.json')

    @staticmethod
    def cleanup_shards(dataset_path):
        "remove non_existing shards from the config"
        dpath = Path(dataset_path)
        fpath = Dataset._get_config_path(dataset_path)
        config = json.loads(open(fpath).read())
        stats = []
        new_shards_list = defaultdict(list)
        for split, slist in config['shards_list'].items():
            kept = 0
            removed = 0
            for s in slist:
                spath = Path(dpath / s['path'])
                if spath.exists():
                    new_shards_list[split].append(s)
                    kept += 1
                else:
                    removed += 1

            stats.append([split, kept, removed])

        # save old config
        sav_path = str(fpath) + ".sav.%d.json" % (time())
        cprint("Saving old config to %s" % sav_path, 'cyan')
        with open(sav_path, 'w+') as o:
            json.dump(config, o)

        config['shards_list'] = new_shards_list
        with open(fpath, 'w+') as o:
            json.dump(config, o)
        cprint("Writing cleaned config", 'green')
        print(tabulate(stats, headers=['split', 'kept', 'removed']))
