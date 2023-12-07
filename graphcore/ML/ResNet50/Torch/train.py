import os
from pathlib import Path
import sys
path_root = Path(__file__).parents[3]
sys.path.append(str(path_root))
import csv
import click
import time

import torch
import torch.distributed as dist
from torchmetrics.classification import Accuracy
from tqdm import tqdm

from ML.gc import GlobalContext
gc = GlobalContext(
    "/work/ta127/ta127/chrisrae/chris-ml-intern/ML/ResNet50/Torch/config.yaml"
)
import ML.ResNet50.Torch.data.data_loader as dl
from ML.ResNet50.Torch.opt import Lars as LARS
from ML.ResNet50.Torch.model.ResNet import ResNet50


def train_step(x, y, model, loss_fn, opt, metric_tracker, batch_idx):
    if (batch_idx+1)% gc["data"]["gradient_accumulation_freq"] != 0:
        if isinstance(model, torch.nn.parallel.DistributedDataParallel):
            with model.no_sync():
                logits = model(x)
                loss = loss_fn(logits, y)/gc["data"]["gradient_accumulation_freq"]
                metric_tracker.update(logits, y)
                loss.backward()
        else:
            logits = model(x)
            loss = loss_fn(logits, y)/gc["data"]["gradient_accumulation_freq"]
            metric_tracker.update(logits, y)
            loss.backward()
    else:
        logits = model(x)
        loss = loss_fn(logits, y)/gc["data"]["gradient_accumulation_freq"]
        metric_tracker.update(logits, y)
        loss.backward()
        opt.step()
        opt.zero_grad()
    return loss


def valid_step(x, y, model, loss_fn, metric_tracker):
    with torch.no_grad():
        logits = model(x)
        loss = loss_fn(logits, y)
        metric_tracker(logits, y)
        return loss


@click.command()
@click.option("--device", "-d", default="", show_default=True, type=str, help="The device type to run the benchmark on (cpu|gpu|cuda). If not provided will default to config.yaml")
@click.option("--config", "-c", default="", show_default=True, type=str, help="Path to config.yaml. If not provided will default to what is provided in train.py")
def main(device, config):
    if device and device.lower() in ('cpu', "gpu", "cuda"):
        gc["device"] = device.lower()
    if config:
        gc.update_config(config)
    
    torch.manual_seed(0)
    if dist.is_mpi_available():
        backend = "mpi"
    elif gc.device == "cuda":
        backend = "nccl"
    else:
        backend = "gloo"
    dist.init_process_group(backend)
    print(f"Hello From Rank {gc.rank}")

    if gc.device == "cuda":
        taskspernode = int(os.environ["SLURM_NTASKS"]) // int(os.environ["SLURM_NNODES"])
        local_rank = int(os.environ["SLURM_PROCID"])%taskspernode
        torch.cuda.set_device("cuda:" + str(local_rank))

    train_data = dl.get_train_dataloader()
    val_data = dl.get_val_dataloader()
    if gc.rank == 0:  # change to -1 to turn off
        train_data = tqdm(train_data, unit="images", unit_scale=(gc["data"]["global_batch_size"] // gc.world_size)//gc["data"]["gradient_accumulation_freq"])
        val_data = tqdm(val_data)

    model = ResNet50(num_classes=1000).to(gc.device)
    if gc.world_size > 1:
        model = torch.nn.parallel.DistributedDataParallel(model)
        if gc.device == "cpu":
            pass
        else:
            model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    if gc["opt"]["name"].upper() == "SGD":
        opt = torch.optim.SGD(
            model.parameters(),
            lr=gc["lr_schedule"]["base_lr"],
            momentum=gc["opt"]["momentum"],
            weight_decay=gc["opt"]["weight_decay"],
        )
    elif gc.opt.name.upper() == "LARS":
        opt = LARS(
            model.parameters(),
            lr=gc["lr_schedule"]["base_lr"],
            momentum=gc["opt"]["momentum"],
            weight_decay=gc["opt"]["weight_decay"],
        )
    else:
        raise ValueError(
            f"Optimiser {gc['opt']['name']} not supported please use SGD|LARS"
        )

    scheduler = torch.optim.lr_scheduler.PolynomialLR(
        opt, total_iters=gc["data"]["n_epochs"], power=gc["lr_schedule"]["poly_power"]
    )

    loss_fn = torch.nn.CrossEntropyLoss()

    train_metric = Accuracy(task="multiclass", num_classes=1000)
    val_metric = Accuracy(task="multiclass", num_classes=1000)

    train_metric.to(gc.device)
    val_metric.to(gc.device)

    model.train()

    E = 1
    while True:
        start = time.time()
        for i, (x, y) in enumerate(train_data):
            x, y = x.to(gc.device), y.to(gc.device)
            loss = train_step(x, y, model, loss_fn, opt, train_metric, i)
        
        train_accuracy = train_metric.compute()
        dist.reduce(train_accuracy, 0)
        total_time = time.time()-start
        total_time = torch.tensor(total_time)
        dist.all_reduce(total_time)
        total_time /= gc.world_size
        if gc.rank == 0:
            print(f"Train Accuracy at Epoch {E}: {train_accuracy/gc.world_size}")
            print(f"Train Loss at Epoch {E}: {loss}")
            dataset_size = gc["data"]["train_subset"] if gc["data"]["train_subset"] else 1000000
            print(f"Processing Speed: {(dataset_size/total_time).item()}")
            with open("./results.csv", "r", newline="") as file:
                reader = csv.reader(file)
                rows = list([row for row in reader])
            with open("./results.csv", "w", newline="") as file:
                writer = csv.writer(file)
                new_row = ["cirrus", gc.device, gc["data"]["global_batch_size"], gc.world_size, int(os.environ["SLURM_NNODES"]), dataset_size, (dataset_size/total_time).item()]
                for row in rows:
                    writer.writerow(row)
                writer.writerow(new_row)
        exit()



        if E % 4 == 0:
            for x, y in val_data:
                loss = valid_step(x, y, model, loss_fn, val_metric)
            val_accuracy = val_metric.compute()
            dist.all_reduce(val_accuracy)
            if gc.rank == 0:
                print(f"Train Accuracy at Epoch {E}: {val_accuracy/gc.world_size}")
                print(f"Validation Loss at Epoch {E}: {loss}")
        E += 1
        if "val_accuracy" in dir(): 
            if E == gc["data"]["n_epochs"] or val_accuracy/gc.world_size >= gc["training"]["target_accuracy"]:
                break
        scheduler.step()


if __name__ == "__main__":
    main()