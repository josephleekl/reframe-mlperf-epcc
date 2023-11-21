from typing import Any
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
        if dist.get_rank() == 0:
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
    def log_cosmoflow(self):
        self.mllogger = mllog.get_mllogger()
        self.mllogger.event(key=log_constants.OPT_NAME, value="SGD")
        self.mllogger.event(key=log_constants.LARS_OPT_MOMENTUM, value=self["opt"]["momentum"])
        self.mllogger.event(key=log_constants.OPT_WEIGHT_DECAY, value=self["opt"]["weight_decay"])
        self.mllogger.event(key="dropout", value=0.5)
        self.mllogger.event(key=log_constants.OPT_BASE_LR, value=self["lr_schedule"]["base_lr"])
        self.mllogger.event(key=log_constants.OPT_LR_WARMUP_EPOCHS, value=self["lr_schedule"]["n_warmup_epochs"])
        self.mllogger.event(key=log_constants.OPT_LR_DECAY_FACTOR, value=max(self["lr_schedule"]["decay_schedule"].values()) if len(self["lr_schedule"]["decay_schedule"])>0 else 1)
        self.log_cluster_info()
    
    @_run_on_0
    def log_deepcam(self):
        self.mllogger = mllog.get_mllogger()
        self.mllogger.event(key=log_constants.OPT_NAME, value=self["opt"]["name"].upper())
        if self["opt"]["name"].upper() == "ADAM":
            self.mllogger.event(key=log_constants.OPT_ADAM_EPSILON, value=1.0e-6)
            self.mllogger.event(key=log_constants.OPT_ADAM_BETA_1, value=self["opt"]["betas"][0])
            self.mllogger.event(key=log_constants.OPT_ADAM_BETA_2, value=self["opt"]["betas"][1])
        elif self["opt"]["name"].upper() == "ADAMW":
            self.mllogger.event(key=log_constants.OPT_ADAMW_EPSILON, value=1.0e-6)
            self.mllogger.event(key=log_constants.OPT_ADAMW_BETA_1, value=self["opt"]["betas"][0])
            self.mllogger.event(key=log_constants.OPT_ADAMW_BETA_2, value=self["opt"]["betas"][1])
        elif self["opt"]["name"].upper() == "LAMB":
            self.mllogger.event(key=log_constants.OPT_LAMB_EPSILON, value=1.0e-6)
            self.mllogger.event(key=log_constants.OPT_LAMB_BETA_1, value=self["opt"]["betas"][0])
            self.mllogger.event(key=log_constants.OPT_LAMB_BETA_2, value=self["opt"]["betas"][1])
        
        self.mllogger.event(key=log_constants.OPT_BASE_LR, value=self["lr_schedule"]["base_lr"])
        self.mllogger.event(key=log_constants.OPT_LR_WARMUP_STEPS, value=self["lr_schedule"]["lr_warmup_steps"])
        self.mllogger.event(key=log_constants.OPT_LR_WARMUP_FACTOR, value=self["lr_schedule"]["lr_warmup_factor"])
        self.mllogger.event(key="scheduler_type", value=self["lr_schedule"]["type"])
        if self["lr_schedule"]["type"].upper() == "MULTISTEP":
            self.mllogger.event(key="scheduler_milestones", value=self["lr_schedule"]["milestones"])
            self.mllogger.event(key=log_constants.OPT_LR_DECAY_FACTOR, value=self["lr_schedule"]["decay_rate"])
        if self["lr_schedule"]["type"].upper() == "COSINE_ANNEALING":
            self.mllogger.event(key="scheduler_t_max", value=self["lr_schedule"]["t_max"])
            self.mllogger.event(key="scheduler_eta_min", value=self["lr_schedule"]["eta_min"])
        
        self.mllogger.event(key="gradient_accumulation_frequency", value=self["data"]["gradient_accumulation"])
        self.log_cluster_info()
    
    @_run_on_0
    def log_cluster_info(self):
        self.mllogger.event(key="number_of_ranks", value=dist.get_world_size())
        self.mllogger.event(key="number_of_nodes", value=int(os.environ["SLURM_NNODES"]))
        self.mllogger.event(key="accelerators_per_node", value=int(os.environ("SLURM_NTASKS_PER_NODE")))

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


