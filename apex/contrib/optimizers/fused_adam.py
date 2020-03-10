import types
import torch
import importlib
from apex.multi_tensor_apply import multi_tensor_applier

class FusedAdam(torch.optim.Optimizer):

    """Implements Adam algorithm. Currently GPU-only.  Requires Apex to be installed via
    ``python setup.py install --cuda_ext --cpp_ext``.

    It has been proposed in `Adam: A Method for Stochastic Optimization`_.

    Arguments:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups.
        lr (float, optional): learning rate. (default: 1e-3)
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square. (default: (0.9, 0.999))
        eps (float, optional): term added to the denominator to improve
            numerical stability. (default: 1e-8)
        weight_decay (float, optional): weight decay (L2 penalty) (default: 0)
        amsgrad (boolean, optional): whether to use the AMSGrad variant of this
            algorithm from the paper `On the Convergence of Adam and Beyond`_
            (default: False) NOT SUPPORTED in FusedAdam!
        eps_inside_sqrt (boolean, optional): in the 'update parameters' step,
            adds eps to the bias-corrected second moment estimate before
            evaluating square root instead of adding it to the square root of
            second moment estimate as in the original paper. (default: False)
        use_mt (boolean, optional): use multi tensor apply for lower launch
            latency. (default: False)
        allow_undo (boolean, optional): allow a step() to be undone automatically
            when overflow is detected in the gradient. This means step() method
            can be called without checking gradient for NaN/Inf first.
            (default: False)

    .. _Adam\: A Method for Stochastic Optimization:
        https://arxiv.org/abs/1412.6980
    .. _On the Convergence of Adam and Beyond:
        https://openreview.net/forum?id=ryQu7f-RZ
    """

    def __init__(self, params,
                 lr=1e-3, bias_correction = True,
                 betas=(0.9, 0.999), eps=1e-8, eps_inside_sqrt = False,
                 weight_decay=0., max_grad_norm=0., amsgrad=False, use_mt=False,
                 amp_scale_adjustment=1.0, allow_undo=False):
        global fused_adam_cuda
        fused_adam_cuda = importlib.import_module("fused_adam_cuda")

        self._use_multi_tensor = False
        if use_mt:
            if not multi_tensor_applier.available:
                print("Warning:  multi_tensor_applier is unavailable")
            else:
                self._use_multi_tensor = True
                self._overflow_buf = torch.cuda.IntTensor([0])

        self._amp_scale_adjustment = amp_scale_adjustment

        if amsgrad:
            raise RuntimeError('FusedAdam does not support the AMSGrad variant.')
        defaults = dict(lr=lr, bias_correction=bias_correction,
                        betas=betas, eps=eps, weight_decay=weight_decay,
                        max_grad_norm=max_grad_norm)
        super(FusedAdam, self).__init__(params, defaults)
        self.eps_mode = 0 if  eps_inside_sqrt else 1

        self._allow_undo = allow_undo
        if not self._use_multi_tensor:
            self._overflow_buf = torch.cuda.IntTensor([0])
        else:
            assert (not self._allow_undo), "Undo feature not supported with multi_tensor mode"

    @property
    def has_overflow(self):
        """Check if overflows were detected by any call to step(...) method.
        Clears the overflow flag.
        """
        has_overflow = self._overflow_buf.item()
        self._overflow_buf.zero_()
        return has_overflow

    @property
    def peek_overflow(self):
        """Check if overflows were detected by any call to step(...) method.
        Does not clear overflow flag.
        """
        return self._overflow_buf.item()

    def strided_check_finite(self, output_params, stride=1, start=-1, end=-1, clear=True):
        """Strided check for overflow.
        You can get status by calling has_overflow.
        """
        if start >= 0 and start < end:
            out_p = output_params[start:end]
        else:
            out_p = output_params
        fused_adam_cuda.strided_check_finite(self._overflow_buf,
                out_p,
                stride,
                1 if clear else 0)

    def _step(self, closure, grads, output_params, scale, grad_norms, undo):
        """Performs a single optimization step.
        """
        if undo:
            assert (self._allow_undo), "Called _step(undo=True) but undo is not supported"
            assert (not self._use_multi_tensor), "Revert step does not support multi tensor"
        else:
            loss = None
            if closure is not None:
                loss = closure()

        if hasattr(self, "_amp_stash"):
            grads = self._amp_stash.grads
            output_params = self._amp_stash.output_params
            scale = self._amp_stash.scale*self._amp_scale_adjustment
            grad_norms = self._amp_stash.grad_norms

        if grads is None:
            grads_group = [None]*len(self.param_groups)
        # backward compatibility
        # assuming a list/generator of parameter means single group
        elif isinstance(grads, types.GeneratorType):
            grads_group = [grads]
        elif type(grads[0])!=list:
            grads_group = [grads]
        else:
            grads_group = grads

        if output_params is None:
            output_params_group = [None]*len(self.param_groups)
        elif isinstance(output_params, types.GeneratorType):
            output_params_group = [output_params]
        elif type(output_params[0])!=list:
            output_params_group = [output_params]
        else:
            output_params_group = output_params

        if grad_norms is None:
            grad_norms = [None]*len(self.param_groups)

        for group, grads_this_group, output_params_this_group, grad_norm in zip(self.param_groups, grads_group, output_params_group, grad_norms):
            if grads_this_group is None:
               grads_this_group = [None]*len(group['params'])
            if output_params_this_group is None:
               output_params_this_group = [None]*len(group['params'])

            # compute combined scale factor for this group
            combined_scale = scale
            if group['max_grad_norm'] > 0:
                # norm is in fact norm*scale
                clip = ((grad_norm / scale) + 1e-6) / group['max_grad_norm']
                if clip > 1:
                    combined_scale = clip * scale

            bias_correction = 1 if group['bias_correction'] else 0

            if self._use_multi_tensor:
                if output_params:
                    tensorlists = [[],[],[],[],[]]
                else:
                    tensorlists = [[],[],[],[]]

            for p, grad, output_param in zip(group['params'], grads_this_group, output_params_this_group):
                #note: p.grad should not ever be set for correct operation of mixed precision optimizer that sometimes sends None gradients
                if p.grad is None and grad is None:
                    continue
                if grad is None:
                    grad = p.grad.data
                if grad.is_sparse:
                    raise RuntimeError('FusedAdam does not support sparse gradients, please consider SparseAdam instead')

                state = self.state[p]

                # State initialization
                if len(state) == 0:
                    state['step'] = 0
                    # Exponential moving average of gradient values
                    state['exp_avg'] = torch.zeros_like(p.data)
                    # Exponential moving average of squared gradient values
                    state['exp_avg_sq'] = torch.zeros_like(p.data)

                exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']
                beta1, beta2 = group['betas']

                if undo:
                    step = state['step']
                    state['step'] -= 1
                else:
                    state['step'] += 1
                    step = state['step']

                if not undo:
                    out_p = torch.tensor([], dtype = torch.float) if output_param is None else output_param
                if self._use_multi_tensor:
                    pl = [p.data, exp_avg, exp_avg_sq, grad]
                    if output_param is not None:
                        pl.append(out_p)

                    for tl, t in zip(tensorlists, pl):
                        tl.append(t)
                else:
                    if undo:
                        fused_adam_cuda.adam_undo(
                                             p.data,
                                             p.data,
                                             exp_avg,
                                             exp_avg,
                                             exp_avg_sq,
                                             exp_avg_sq,
                                             grad,
                                             group['lr'],
                                             beta1,
                                             beta2,
                                             group['eps'],
                                             combined_scale,
                                             step,
                                             self.eps_mode,
                                             bias_correction,
                                             group['weight_decay'])
                    else:
                        fused_adam_cuda.adam(self._overflow_buf,
                                             p.data,
                                             p.data,
                                             out_p,
                                             exp_avg,
                                             exp_avg,
                                             exp_avg_sq,
                                             exp_avg_sq,
                                             grad,
                                             group['lr'],
                                             beta1,
                                             beta2,
                                             group['eps'],
                                             combined_scale,
                                             step,
                                             self.eps_mode,
                                             bias_correction,
                                             group['weight_decay'])

            if self._use_multi_tensor:
                multi_tensor_applier(
                    fused_adam_cuda.adam_mt,
                    self._overflow_buf,
                    tensorlists,
                    group['lr'],
                    beta1,
                    beta2,
                    group['eps'],
                    combined_scale,
                    state['step'],
                    self.eps_mode,
                    bias_correction,
                    group['weight_decay'])

        return None if undo else loss

    def step(self, closure=None, grads=None, output_params=None, scale=1., grad_norms=None):
        """Performs a single optimization step.

        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
            grads (list of tensors, optional): weight gradient to use for the
                optimizer update. If gradients have type torch.half, parameters
                are expected to be in type torch.float. (default: None)
            output params (list of tensors, optional): A reduced precision copy
                of the updated weights written out in addition to the regular
                updated weights. Have to be of same type as gradients. (default: None)
            scale (float, optional): factor to divide gradient tensor values
                by before applying to weights. (default: 1)
        """
        self._overflow_buf.zero_()
        loss = self._step(closure, grads, output_params, scale, grad_norms, False)
        if self._allow_undo and self.peek_overflow:
            # revert step
            self._step(closure, grads, output_params, scale, grad_norms, True)

