FROM nvcr.io/nvidia/pytorch:23.03-py3

RUN pip install torchmetrics
RUN pip install "git+https://github.com/mlperf/logging.git"

COPY . .

