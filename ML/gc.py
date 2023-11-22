import yaml
import os
from contextlib import contextmanager

import torch.distributed as dist
from torch.profiler import profile, record_function, ProfilerActivity
from mlperf_logging import mllog
from mlperf_logging.mllog import constants as log_constants

class SingletonMetaClass(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(SingletonMetaClass, cls).__call__(*args, **kwargs)
        return cls._instances[cls]

def _run_on_0(func):
    def wrapper(*args, **kwrags):
        if "sync" in kwrags.keys():
            if kwrags["sync"]:
                dist.barrier()
        if dist.get_global_rank() == 0:
            return func(*args, **kwrags)
        return wrapper

class GlobalContext(dict, metaclass=SingletonMetaClass):
    _config_path = None
    """
    reads the yaml files and stores data as its parameters

    being a singleton class prevents having to read the yaml file every time
    """
    def __init__(self, config_path=None):
        if not self.__dict__:
            with open(config_path, "r") as stream:
                self.clear()
                self.update(yaml.safe_load(stream))
                if self["device"].lower() == 'gpu':
                    self["device"] = "cuda"
            
    @property
    def rank(self):
        return dist.get_rank()
    
    @property
    def world_size(self):
        return dist.get_world_size()
    
    @property
    def device(self):
        return self["device"].lower()
    
    def update_config(self, config_path):
        with open(config_path, "r") as stream:
            self.__dict__.clear()
            self.__dict__.update(yaml.safe_load(stream))
            if self["device"].lower() == 'gpu':
                self["device"] = "cuda"
    
    @_run_on_0
    def log_bert(self):
        mllogger = mllog.get_mllogger()
        self.mllogger.default_namespace = "bert"
        mllogger.event(key=log_constants.BERT)
        mllogger.event(key=log_constants.OPT_NAME, value=self["opt"]["name"])
        mllogger.event(key=log_constants.GLOBAL_BATCH_SIZE, value=self["data"]["global_batch_size"])
        mllogger.event(key=log_constants.OPT_BASE_LR, value=self["lr_schedule"]["base_lr"])
        mllogger.event(key=log_constants.OPT_LAMB_EPSILON, value=1.0e-6)
        mllogger.event(key=log_constants.OPT_LR_TRAINING_STEPS, value=self["lr_schedule"]["total_steps"])
        mllogger.event(key=log_constants.OPT_LR_WARMUP_STEPS, value=self["lr_schedule"]["lr_warmup_steps"])
        mllogger.event(key=log_constants.NUM_WARMUP_STEPS, value=self["lr_schedule"]["lr_warmup_steps"])
        mllogger.event(key=log_constants.START_WARMUP_STEP, value=self["lr_schedule"]["start_warmup_step"])
        mllogger.event(key=log_constants.OPT_LAMB_BETA_1, value=self["opt"]["betas"][0])
        mllogger.event(key=log_constants.OPT_LAMB_BETA_2, value=self["opt"]["betas"][1])
        mllogger.event(key=log_constants.OPT_WEIGHT_DECAY, value=self["self"]["weight_decay"])
        self.log_cluster_info()
    
    @_run_on_0
    def log_resnet(self):
        mllogger = mllog.get_mllogger()
        self.mllogger.default_namespace = "resnet"
        mllogger.event(key=log_constants.RESNET)
        if self["opt"]["name"].upper() == "SGD":
            mllogger.event(key=log_constants.OPT_NAME, value=self["opt"]["name"].upper())
        elif self["opt"]["name"].upper() == "LARS":
            mllogger.event(key=log_constants.OPT_NAME, value=self["opt"]["name"].upper())
            mllogger.event(key=log_constants.LARS_EPSILON, value=1.0e-6)
        
        mllogger.event(key=log_constants.GLOBAL_BATCH_SIZE, value=self["data"]["global_batch_size"])
        mllogger.event(key=log_constants.OPT_BASE_LR, value=self["lr_schedule"]["base_lr"])
        mllogger.event(key=log_constants.OPT_END_LR, value=self["lr_schedule"]["end_lr"])
        mllogger.event(key=log_constants.LARS_OPT_LR_DECAY_POLY_POWER, value=self["lr_schedule"]["poly_power"])
        mllogger.event(key=log_constants.OPT_LR_DECAY_STEPS, value=self["lr_schedule"]["decay_steps"])
        mllogger.event(key=log_constants.LARS_OPT_MOMENTUM, value=self["opt"]["momentum"])
        mllogger.event(key=log_constants.OPT_WEIGHT_DECAY, value=self["opt"]["weight_decay"])
        self.log_cluster_info()

    @_run_on_0
    def log_cluster_info(self):
        self.mllogger.event(key="number_of_ranks", value=dist.get_world_size())
        self.mllogger.event(key="number_of_nodes", value=int(os.environ["SLURM_NNODES"]))
        self.mllogger.event(key="accelerators_per_node", value=int(os.environ["SLURM_NTASKS_PER_NODE"]))

    @_run_on_0
    def print_0(self, *args, **kwargs):
        print(*args, **kwargs)
    
    @contextmanager
    def profiler(self, name: str):
        if self.device == "cpu":
            activities=[ProfilerActivity.CPU]
        else:
            activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA]
        with profile(activities=activities, with_flops=True) as prof:
            with record_function(name):
                yield prof
    
    @_run_on_0
    def log_event(self, *args, sync=True, **kwargs):
        self.mllogger.event(*args, **kwargs)
    
    @_run_on_0
    def log_seed(self, seed, sync=True):
        self.mllogger.event(key=log_constants.SEED, value=seed)

    @_run_on_0
    def start_init(self, sync=True):
        self.mllogger.start(key=log_constants.INIT_START, value=None)
    
    @_run_on_0
    def stop_init(self, sync=True):
        self.mllogger.end(key=log_constants.INIT_STOP, value=None)
    
    @_run_on_0
    def start_run(self, sync=True):
        self.mllogger.start(key=log_constants.RUN_START, value=None)
    
    @_run_on_0
    def stop_run(self, metadata = {"status": "success"}, sync=True):
        self.mllogger.end(key=log_constants.RUN_STOP, value=None, metadata=metadata)
    
    @_run_on_0
    def start_epoch(self, metadata, sync=True):
        self.mllogger.start(key=log_constants.EPOCH_START, value=None, metadata=metadata)

    @_run_on_0
    def stop_epoch(self, metadata, sync=True):
        self.mllogger.end(key=log_constants.EPOCH_STOP, value=None, metadata=metadata)
    
    @_run_on_0
    def start_eval(self, metadata, sync=True):
        self.mllogger.start(key=log_constants.EVAL_START, value=None, metadata=metadata)
    
    @_run_on_0
    def stop_eval(self, metadata, sync=True):
        self.mllogger.end(key=log_constants.EVAL_STOP, value=None, metadata=metadata)