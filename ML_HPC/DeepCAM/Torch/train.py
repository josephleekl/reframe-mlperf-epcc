import os
from pathlib import Path
import sys
path_root = Path(__file__).parents[3]
sys.path.append(str(path_root))
import time
import warnings
warnings.filterwarnings("ignore")
import click

import torch
import torch.distributed as dist
import torch.nn as nn

from ML_HPC.gc import GlobalContext
gc = GlobalContext("/work/ta127/ta127/chrisrae/chris-ml-intern/ML_HPC/DeepCAM/Torch/config.yaml")
import ML_HPC.DeepCAM.Torch.data.data_loader as dl
from ML_HPC.DeepCAM.Torch.model.DeepCAM import DeepLabv3_plus
from ML_HPC.DeepCAM.Torch.lr_scheduler.schedulers import MultiStepLRWarmup, CosineAnnealingLRWarmup
from ML_HPC.DeepCAM.Torch.optimizer.lamb import Lamb
from ML_HPC.DeepCAM.Torch.validation import validate, compute_score


class CELoss(nn.Module):

    def __init__(self, weight):

        # init superclass
        super(CELoss, self).__init__()

        # instantiate loss
        self.criterion = nn.CrossEntropyLoss(weight=torch.tensor(weight).to(torch.float32), reduction='none')

    def forward(self, logit, target):

        # squeeze target
        target = target.squeeze(1)
   
        # get losses and predictions
        losses = self.criterion(logit, target)

        # average
        loss = torch.mean(losses)

        return loss

def dummy_loaders(n_samples):
    bs = (gc["data"]["global_batch_size"] // gc.world_size)//gc["data"]["gradient_accumulation"]
    yield torch.ones(bs, 16, 768, 1152),  torch.ones(bs, 1, 768, 1152), "file.txt"

@click.command()
@click.option("--device", "-d", default="", show_default="", type=str, help="The device type to run the benchmark on (cpu|gpu|cuda). If not provided will default to config.yaml")
@click.option("--config", "-c", default="", show_default="", type=str, help="Path to config.yaml. If not provided will default to what is provided in train.py")
def main(device, config):
    if device and device.lower() in ('cpu', "gpu", "cuda"):
        gc["device"] = device.lower()
    if config:
        gc.update_config(config)


    torch.manual_seed(333)
    if dist.is_mpi_available():
        backend = "mpi"
    elif gc.device == "cuda":
        backend = "nccl"
    else:
        backend = "gloo"
    dist.init_process_group(backend)

    gc.log_deepcam()
    gc.log_seed(333)

    if gc.device == "cuda":
        local_rank = os.environ["LOCAL_RANK"]
        torch.cuda.set_device("cuda:" + local_rank)
    
    gc.start_init()
    
    train_data, train_data_size, val_data, val_data_size = dl.get_dataloaders()  

    model = DeepLabv3_plus(n_input=16, n_classes=3, pretrained=False, rank=gc.rank, process_group=None).to(gc.device)
    if gc.world_size > 1:
        model = torch.nn.parallel.DistributedDataParallel(model)

    if gc["opt"]["name"].upper() == "ADAM":
        opt = torch.optim.Adam(model.parameters(), lr=gc["lr_schedule"]["base_lr"], betas=gc["opt"]["betas"], eps=1e-6, weight_decay=gc["opt"]["weight_decay"])
    elif gc["opt"]["name"].upper() == "ADAMW":
        opt = torch.optim.AdamW(model.parameters(), lr=gc["lr_schedule"]["base_lr"], betas=gc["opt"]["betas"], eps=1e-6, weight_decay=gc["opt"]["weight_decay"])
    elif gc["opt"]["name"].upper() == "LAMB":
        opt = Lamb(model.parameters(), lr=gc["lr_schedule"]["base_lr"], betas=gc["opt"]["betas"], eps=1e-6, weight_decay=gc["opt"]["weight_decay"])
    else:
        raise NameError(f"Optimiser {gc['opt']['name']} not supported please use ADAM|ADAMW|LAMB")
    
    if gc["lr_schedule"]["type"] == "multistep":
        if gc["lr_schedule"]["lr_warmup_steps"] > 0:
            scheduler = MultiStepLRWarmup(opt, last_epoch=-1) # will have to change L_E once checkpointing supported
        else:
            scheduler = torch.optim.lr_scheduler.MultiStepLR(opt, 
                                                             milestones=[gc["lr_schedule"]["milestones"]], 
                                                             gamma=gc["lr_schedule"]["decay_rate"], 
                                                             last_epoch=-1)

    elif gc["lr_schedule"]["type"] == "cosine_annealing":
        if gc["lr_schedule"]["lr_warmup_steps"] > 0:
            scheduler = CosineAnnealingLRWarmup(opt, last_epoch=-1)
        else:
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(opt,
                                                                  T_max=gc["lr_schedule"]["t_max"],
                                                                  eta_min=gc["lr_schedule"]["eta_min"],
                                                                  last_epoch=-1)

    loss_pow = -0.125
    class_weights = [0.986267818390377**loss_pow, 0.0004578708870701058**loss_pow, 0.01327431072255291**loss_pow]
    criterion = CELoss(class_weights).to(gc.device)
    
    gc.start_init()
    gc.start_run()

    epoch = 0
    stop_training = False
    model.train()
    # Train Loop
    print("Training Started \n")
    while True:
        gc.start_epoch(metadata={"epoch_num": epoch+1})
        train_data.sampler.set_epoch(epoch)
        model.train()
    
        for idx, (x, y, _) in enumerate(train_data):
            x, y = x.to(gc.device), y.to(gc.device)
            logits = model.forward(x)
            loss = criterion.forward(logits, y)/gc["data"]["gradient_accumulation"]
            loss.backward()
            if ((idx + 1)%gc["data"]["gradient_accumulation"]==0) or (idx+1 == len(train_data)):
                opt.step()
                opt.zero_grad(set_to_none=True)
                scheduler.step()
        

            loss_avg = loss.detach()
            if dist.is_initialized():
                dist.reduce(loss_avg, dst=0, op=dist.ReduceOp.SUM)
            loss_avg_train = loss_avg.item() / float(gc.world_size)

            predictions = torch.argmax(torch.softmax(logits, 1), 1)
            iou = compute_score(predictions, y, num_classes=3)
            iou_avg = iou.detach()
            if dist.is_initialized():
                dist.reduce(iou_avg, dst=0, op=dist.ReduceOp.SUM)
            iou_avg_train = iou_avg.item() / float(gc.world_size)

            gc.log_event(key="learning_rate", value=scheduler.get_last_lr()[0], metadata={"epoch_num": epoch+1})
            gc.log_event(key="training_accuracy", value=iou_avg_train, metadata={"epoch_num": epoch+1})
            gc.log_event(key="train_loss", value=loss_avg_train, metadata={"epoch": epoch+1})


        stop_training = validate(model, criterion, val_data, epoch)
        gc.stop_epoch(metadata={"epoch_num": epoch+1})
        epoch += 1

        if stop_training or epoch == gc["data"]["n_epochs"]:
            gc.stop_run()
            break

if __name__ == "__main__":
    main()
    

    