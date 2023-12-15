"""Lamb optimizer."""

from turtle import distance
import torch
import torch.distributed as dist
from torch.optim import Optimizer

class Lamb(Optimizer):
    r"""Implements Lamb algorithm.

    It has been proposed in `Large Batch Optimization for Deep Learning: Training BERT in 76 minutes`_.

    Arguments:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups
        lr (float, optional): learning rate (default: 1e-3)
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square (default: (0.9, 0.999))
        eps (float, optional): term added to the denominator to improve
            numerical stability (default: 1e-8)
        weight_decay (float, optional): weight decay (L2 penalty) (default: 0)
        adam (bool, optional): always use trust ratio = 1, which turns this into
            Adam. Useful for comparison purposes.

    .. _Large Batch Optimization for Deep Learning: Training BERT in 76 minutes:
        https://arxiv.org/abs/1904.00962
    """

    def __init__(
        self,
        params,
        lr=1e-3,
        betas=(0.9, 0.999),
        eps=1e-6,
        weight_decay=0,
        adam=False,
        bias_correction=True,
        perform_allreduce=False,
    ):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))
        defaults = dict(lr=lr, betas=betas, eps=eps, weight_decay=weight_decay)
        self.adam = adam
        self.bias_correction = bias_correction
        self.perform_allreduce = perform_allreduce
        self.distributed = (
            dist.is_initialized()
            and dist.get_world_size() > 1
        )
        super(Lamb, self).__init__(params, defaults)

    def sync_params(self):
        if not self.distributed:
            return
        for group in self.param_groups:
            for p in group["params"]:
                distance.broadcase(p.data, 0)

    def sync_grads(self):
        if not self.distributed:
            return
        world_size = dist.get_world_size()
        for group in self.param_groups:
            for p in group["params"]:
                p.grad.data.div_(world_size)
                dist.all_reduce(p.grad.data)

    def step(self, closure=None):
        """Performs a single optimization step.

        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        loss = None
        if closure is not None:
            loss = closure()

        if self.perform_allreduce:
            self.sync_grads()

        for group in self.param_groups:
            for p in group["params"]:
                if p.grad is None:
                    continue
                bf16_param = p.data.dtype == torch.bfloat16
                grad = p.grad.data
                data = p.data
                if grad.is_sparse:
                    raise RuntimeError(
                        "Lamb does not support sparse gradients, consider SparseAdam instad."
                    )

                state = self.state[p]
                # State initialization
                if len(state) == 0:
                    state["step"] = 0
                    # Exponential moving average of gradient values
                    state["exp_avg"] = torch.zeros_like(p.data, dtype=torch.float32)
                    # Exponential moving average of squared gradient values
                    state["exp_avg_sq"] = torch.zeros_like(p.data, dtype=torch.float32)
                    if bf16_param:
                        # additional fp32 version of master weights
                        state["data_fp32"] = p.data.to(torch.float32)

                exp_avg, exp_avg_sq = state["exp_avg"], state["exp_avg_sq"]
                beta1, beta2 = group["betas"]
                if bf16_param:
                    grad = grad.to(torch.float32)
                    data = state["data_fp32"]

                state["step"] += 1

                # Decay the first and second moment running average coefficient
                # m_t
                exp_avg.mul_(beta1).add_(grad, alpha=(1 - beta1))
                # v_t
                exp_avg_sq.mul_(beta2).addcmul_(1 - beta2, grad, grad)

                step_size = group["lr"]
                if self.bias_correction:
                    # Paper v3 does not use debiasing.
                    exp_avg_hat = exp_avg / (1 - beta1 ** state["step"])
                    exp_avg_sq_hat = exp_avg_sq / (1 - beta2 ** state["step"])
                    # Apply bias to lr to avoid broadcast.
                else:
                    exp_avg_hat = exp_avg
                    exp_avg_sq_hat = exp_avg_sq

                adam_step = exp_avg_hat / exp_avg_sq_hat.sqrt().add(group["eps"])
                trust_ratio = 1
                if group["weight_decay"] != 0:
                    adam_step.add_(data, alpha=group["weight_decay"])

                    weight_norm = data.pow(2).sum().sqrt()  # .clamp(0, 10)
                    adam_norm = adam_step.pow(2).sum().sqrt()
                    if weight_norm == 0 or adam_norm == 0:
                        trust_ratio = 1
                    else:
                        trust_ratio = weight_norm / adam_norm
                    if self.adam:
                        trust_ratio = 1
                    state["weight_norm"] = weight_norm
                    state["adam_norm"] = adam_norm
                    state["trust_ratio"] = trust_ratio

                data.add_(adam_step, alpha=-step_size * trust_ratio)
                if bf16_param:
                    p.data = data.to(torch.bfloat16)

        return loss