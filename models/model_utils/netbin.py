
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Function

import weakref
import functools


#from utils import ModelHelper


class ModelHelper:
    """a mixin class to add basic functionality to torch modules

    Note: args for the network constructor should be passed via ``kwargs`` in
     :meth:`create_with_load`, so it can be saved in the dict.
    """

    __ctor_args = None
    _ctor_args_key = '__ModelHelper_ctor_args'

    def save_to_file(self, fpath, state_only=False):
        if isinstance(fpath, Path):
            fpath = str(fpath)
        if state_only:
            state = self.state_dict()
            state[self._ctor_args_key] = self.__ctor_args
        else:
            state = self
        #print(state)
        torch.save(state, fpath)

    @classmethod
    def create_with_load(cls, fpath=None, kwargs={}, enforce_state_load=False):
        """note that ``fpath`` can either contain a full model or only the state
        dict

        :param enforce_state_load: if the complete model class is saved, whether
            to load only the states and assign to this class, or return the
            loaded class directly
        """
        if fpath is not None:
            if isinstance(fpath, Path):
                fpath = str(fpath)
            print(fpath)
            state = torch.load(fpath, map_location=torch.device('cpu'))
            if isinstance(state, ModelHelper):
                if enforce_state_load:
                    state = state.state_dict()
                else:
                    state._on_load_from_file()
                    return state
            kwargs = kwargs.copy()
            kwargs.update(state.pop(cls._ctor_args_key, {}))
            ret = cls(**kwargs)
            ret.load_state_dict(state)
            return ret
        else:
            ret = cls(**kwargs)
            ret.__ctor_args = kwargs
            w = getattr(ret, 'weights_init', None)
            if w is not None:
                ret.apply(w)
                print('custom weight init used for {}'.format(ret))
            return ret

    def _on_load_from_file(self):
        """can be overriden by subclasses to get notification when model is
        loaded from file"""

g_weight_decay = 1e-7
g_channel_scale = 1
g_bingrad_soft_tanh_scale = 1
g_weight_mask_std = 0.01
g_use_scalar_scale_last_layer = True
g_remove_last_bn = False

def scale_channels(x: int):
    return max(int(round(x * g_channel_scale)), 1)

class AbstractTensor:
    """tensor object for abstract interpretation (here we use interval
    arithmetic)
    """
    __slots__ = ['vmin', 'vmax', 'loss']

    loss_layer_decay = 1
    """decay of loss of previous layer compared to current layer"""

    def __init__(self, vmin: torch.Tensor, vmax: torch.Tensor,
                 loss: torch.Tensor):
        assert vmin.shape == vmax.shape and loss.numel() == 1
        self.vmin = vmin
        self.vmax = vmax
        self.loss = loss

    def apply_linear(self, w, func):
        """apply a linear function ``func(self, w)`` by decomposing ``w`` into
        positive and negative parts"""
        wpos = F.relu(w)
        wneg = w - wpos
        vmin_new = func(self.vmin, wpos) + func(self.vmax, wneg)
        vmax_new = func(self.vmax, wpos) + func(self.vmin, wneg)
        return AbstractTensor(torch.min(vmin_new, vmax_new),
                              torch.max(vmin_new, vmax_new),
                              self.loss)

    def apply_elemwise_mono(self, func):
        """apply a non-decreasing monotonic function on the values"""
        return AbstractTensor(func(self.vmin), func(self.vmax), self.loss)

    @property
    def ndim(self):
        return self.vmin.ndim

    @property
    def shape(self):
        return self.vmin.shape

    def size(self, dim):
        return self.vmin.size(dim)

    def view(self, *shape):
        return AbstractTensor(self.vmin.view(shape), self.vmax.view(shape),
                              self.loss)


class MultiSampleTensor:
    """tensor object that contains multiple samples within the perturbation
    bound near the natural image

    :var data: a tensor ``(K * N, C, H, W)`` where ``data[0:N]`` is the natural
        image, and data[N::N] are perturbations
    """
    __slots__ = ['k', 'data', 'loss']

    loss_layer_decay = 1
    """decay of loss of previous layer compared to current layer"""

    def __init__(self, k: int, data: torch.Tensor, loss: torch.Tensor = 0):
        self.k = k
        self.data = data
        self.loss = loss
        assert data.shape[0] % k == 0

    @classmethod
    def from_squeeze(cls, data: torch.Tensor, loss=0):
        """``data`` should be in ``(K, N, ...)`` format"""
        k, n, *other = data.shape
        return cls(k, data.view(k * n, *other), loss)

    def apply_batch(self, func, loss=None):
        """apply a batch function"""
        if loss is None:
            loss = self.loss
        return MultiSampleTensor(self.k, func(self.data), loss)

    def as_expanded_tensor(self) -> torch.Tensor:
        """expand the first dimension to ``(K, N)``"""
        kn, *other = self.shape
        k = self.k
        n = kn // k
        return self.data.view(k, n, *other)

    @property
    def ndim(self):
        return self.data.ndim

    @property
    def shape(self):
        return self.data.shape

    def size(self, dim):
        return self.data.size(dim)

    def view(self, *shape):
        assert shape[0] == self.shape[0]
        return MultiSampleTensor(self.k, self.data.view(shape), self.loss)


class Binarize01Act(nn.Module):
    class Fn(Function):
        @staticmethod
        def forward(ctx, inp, scale=None):
            """:param scale: scale for gradient computing"""
            if scale is None:
                ctx.save_for_backward(inp)
            else:
                ctx.save_for_backward(inp, scale)
            return (inp >= 0).to(inp.dtype)

        @staticmethod
        def backward(ctx, g_out):
            if len(ctx.saved_tensors) == 2:
                inp, scale = ctx.saved_tensors
            else:
                inp, = ctx.saved_tensors
                scale = 1

            if g_bingrad_soft_tanh_scale is not None:
                scale = scale * g_bingrad_soft_tanh_scale
                tanh = torch.tanh_(inp * scale)
                return (1 - tanh.mul_(tanh)).mul_(g_out), None

            # grad as sign(hardtanh(x))
            g_self = (inp.abs() <= 1).to(g_out.dtype)
            return g_self.mul_(g_out), None

    def __init__(self, grad_scale=1):
        super().__init__()
        self.register_buffer(
            'grad_scale',
            torch.tensor(float(grad_scale), dtype=torch.float32))

    def forward(self, x):
        grad_scale = getattr(self, 'grad_scale', None)
        f = lambda x: self.Fn.apply(x, grad_scale)

        def rsloss(x, y):
            return (1 - torch.tanh(1 + x * y)).sum()

        if type(x) is AbstractTensor:
            loss = rsloss(x.vmin, x.vmax)
            loss += x.loss * AbstractTensor.loss_layer_decay
            vmin = f(x.vmin)
            vmax = f(x.vmax)
            return AbstractTensor(vmin, vmax, loss)
        elif type(x) is MultiSampleTensor:
            rv = x.as_expanded_tensor()
            loss = rsloss(rv[-1], rv[-2])
            return x.apply_batch(
                f,
                loss=x.loss * MultiSampleTensor.loss_layer_decay + loss
            )
        else:
            return f(x)

class activation_quantize_fn2(nn.Module):
  def __init__(self, a_bit=2):
    super(activation_quantize_fn2, self).__init__()
    assert a_bit <= 8 or a_bit == 32
    self.a_bit = a_bit
    self.uniform_q = uniform_quantize2(k=a_bit)

    self.coef = 2**a_bit-1

  def forward(self, x):
    if self.a_bit == 32:
      activation_q = x
    else:
      activation_q = self.coef * self.uniform_q(torch.clamp(x, 0, 1))
      #print(activation_q)
      # print(np.unique(activation_q.detach().numpy()))
    return activation_q


class activation_quantize_fn8(nn.Module):
  def __init__(self, a_bit=8):
    super(activation_quantize_fn8, self).__init__()
    assert a_bit <= 8 or a_bit == 32
    self.a_bit = a_bit
    self.uniform_q = uniform_quantize2(k=a_bit)

    self.coef = 2**a_bit-1

  def forward(self, x):
    if self.a_bit == 32:
      activation_q = x
    else:
      activation_q = self.coef * self.uniform_q(torch.clamp(x, 0, 1))
      #print(activation_q)
      # print(np.unique(activation_q.detach().numpy()))
    return activation_q

def uniform_quantize2(k):
  class qfn2(torch.autograd.Function):

    @staticmethod
    def forward(ctx, input):
      if k == 32:
        out = input
      elif k == 1:
        out = torch.sign(input)
      else:
        n = float(2 ** k - 1)
        out = torch.round(input * n) / n
      return out

    @staticmethod
    def backward(ctx, grad_output):
      grad_input = grad_output.clone()
      return grad_input

  return qfn2().apply

class Binarize01WeightNoScaleFn(Function):
    @staticmethod
    def forward(ctx, inp):
        v = (inp >= 0).to(inp.dtype)
        ctx.save_for_backward(v)
        return v

    @staticmethod
    def backward(ctx, g_out):
        out, = ctx.saved_tensors
        return (out * g_weight_decay).add_(g_out)


class TernaryWeightFn(Function):
    @staticmethod
    def forward(ctx, inp):
        v = inp.sign().mul_((inp.abs() >= 0.005).to(inp.dtype))
        ctx.save_for_backward(v)
        return v

    @staticmethod
    def backward(ctx, g_out):
        out, = ctx.saved_tensors
        return (out * g_weight_decay).add_(g_out)


class TernaryWeightWithMaskFn(Function):
    """BinMask in the paper; see :func:`binarize_weights`"""
    @staticmethod
    def forward(ctx, inp):
        return inp.sign()

    @staticmethod
    def backward(ctx, g_out):
        return g_out


class IdentityWeightFn(Function):
    """using the original floating point weight"""
    @staticmethod
    def forward(ctx, inp):
        return inp

    @staticmethod
    def backward(ctx, g_out):
        return g_out


class Quant3WeightFn(Function):
    """quantized weight with values in the range [-3, 3]"""

    @staticmethod
    def forward(ctx, inp: torch.Tensor):
        qmin = -0.016
        qmax = 0.016
        # 90 interval of 0.01 normal distribution
        step = (qmax - qmin) / 7
        return torch.clamp((inp - qmin).div_(step).floor_().sub_(3), -3, 3)

    @staticmethod
    def backward(ctx, g_out):
        return g_out

class Quant3WeightWithMaskFn(Quant3WeightFn):
    pass


g_weight_binarizer = TernaryWeightWithMaskFn
g_weight_binarizer2 = IdentityWeightFn #Quant3WeightFn
g_weight_binarizer3 = Quant3WeightFn

def binarize_weights(layer: nn.Module):
    weight: torch.Tensor = layer.weight
    wb = layer.weight_binarizer
    if wb is TernaryWeightWithMaskFn or wb is Quant3WeightWithMaskFn or  wb is IdentityWeightFn:
        mask = layer._parameters.get('weight_mask')
        if mask is None:
            layer.register_parameter(
                'weight_mask', nn.Parameter(
                    torch.empty_like(weight).
                    normal_(std=g_weight_mask_std).
                    abs_().
                    detach_()
                ))
            mask = layer._parameters['weight_mask']
        return wb.apply(weight) * Binarize01WeightNoScaleFn.apply(mask)

    assert wb in [TernaryWeightFn, IdentityWeightFn, Quant3WeightFn]
    return wb.apply(weight)


class BinConv2d(nn.Conv2d):
    """conv with binarized weights; no bias is allowed"""

    rounding = False

    class RoundFn(Function):
        """apply rounding to compensate computation errors in float conv"""
        @staticmethod
        def forward(ctx, inp):
            return torch.round_(inp)

        @staticmethod
        def backward(ctx, g_out):
            return g_out

        @classmethod
        def g_apply(cls, inp):
            """general apply with handling of :class:`AbstractTensor`"""
            f = cls.apply
            if type(inp) is AbstractTensor:
                return inp.apply_elemwise_mono(f)
            if type(inp) is MultiSampleTensor:
                return inp.apply_batch(f)
            return f(inp)


    def __init__(self, weight_binarizer, in_channels, out_channels, kernel_size,
                 stride=1, padding=0, groups =1,  bias=False, rounding=True):
        """:param rounding: whether the output should be rounded to integer to
            compensate float computing errors. It should be set when input is
            guaranteed to be int.
        """
        super().__init__(in_channels, out_channels, kernel_size,
                         stride=stride, padding=padding, groups=groups, bias=bias)
        self.weight_binarizer = weight_binarizer
        binarize_weights(self)  # create weight_mask
        self.rounding = rounding

    def _do_forward(self, x, *, weight=None, bias=None):
        if weight is None:
            weight = self.weight_bin

        def do_conv(x, w):
            return F.conv2d(x, w, bias, self.stride,
                            self.padding, self.dilation, self.groups)

        if type(x) is AbstractTensor:
            return x.apply_linear(weight, do_conv)

        if type(x) is MultiSampleTensor:
            return x.apply_batch(lambda d: do_conv(d, weight))

        return do_conv(x, weight)

    def forward(self, x):
        y = self._do_forward(x)
        if self.rounding:
            y = self.RoundFn.g_apply(y)
        return y

    @property
    def weight_bin(self):
        return binarize_weights(self)

    def reset_parameters(self):
        with torch.no_grad():
            self.weight.normal_(std=0.01)

    def __repr__(self):
        kh, kw = self.weight.shape[2:]
        return (
            f'{type(self).__name__}({self.in_channels}, {self.out_channels}, '
            f'bin={self.weight_binarizer.__name__}, '
            f'kern=({kh}, {kw}), stride={self.stride}, padding={self.padding})'
        )


class BinLinear(nn.Linear):
    rounding = False

    RoundFn = BinConv2d.RoundFn

    def __init__(self, weight_binarizer, in_features, out_features,
                 rounding=True):
        super().__init__(in_features, out_features, bias=False)
        self.weight_binarizer = weight_binarizer
        binarize_weights(self)  # create weight_mask
        self.rounding = rounding

    def _do_forward(self, x, *, weight=None, bias=None):
        if weight is None:
            weight = self.weight_bin

        matmul = functools.partial(F.linear, bias=bias)

        if type(x) is AbstractTensor:
            return x.apply_linear(weight, matmul)

        if type(x) is MultiSampleTensor:
            return x.apply_batch(lambda d: matmul(d, weight))
        #print(x.shape, weight.shape)
        return matmul(x, weight)

    def forward(self, x):
        y = self._do_forward(x)
        if self.rounding:
            y = self.RoundFn.g_apply(y)
        return y

    @property
    def weight_bin(self):
        return binarize_weights(self)

    def reset_parameters(self):
        with torch.no_grad():
            self.weight.normal_(std=0.01)


class PositiveInputCombination:
    @classmethod
    def bias_from_bin_weight(cls, weight):
        return F.relu_(-weight.view(weight.size(0), -1)).sum(dim=1)

    def get_bias(self):
        """equivalent to ``self.bias_from_bin_weight(self.weight_bin)``"""
        return self.bias_from_bin_weight(self.weight_bin)


class BinConv2dPos(BinConv2d, PositiveInputCombination):
    """binarized conv2d where the output is always positive (i.e. treat -1
    weight as adding the bool negation of input var)"""

    def _do_forward(self, x):
        weight_bin = self.weight_bin
        return super()._do_forward(
            x, weight=weight_bin,
            bias=self.bias_from_bin_weight(weight_bin))


class BinLinearPos(BinLinear, PositiveInputCombination):
    def _do_forward(self, x):
        weight_bin = self.weight_bin
        #print(weight_bin)
        return super()._do_forward(
            x, weight=weight_bin,
            bias=self.bias_from_bin_weight(weight_bin))


class ScaleBias(nn.Module):
    """scale features and add bias for classfication"""

    def __init__(self, nr_classes):
        super().__init__()
        self.nr_classes = nr_classes
        self.scale = nn.Parameter(
            torch.from_numpy(np.array(1 / nr_classes, dtype=np.float32)))
        self.bias = nn.Parameter(
            torch.from_numpy(np.zeros(nr_classes, dtype=np.float32))
        )

    def forward(self, x):
        return self.scale * x + self.bias.view(-1, self.nr_classes)

    def get_scale_bias_eval(self):
        return self.scale, self.bias


class BatchNormStatsCallbak(nn.Module):
    """batchnorm with callback for scale and bias:
    ``owner.on_bn_internals(bn, scale, bias)`` would be called each time
    :meth:`forward` gets executed
    """
    use_scalar_scale = False
    bias_regularizer_coeff = 1

    def __init__(self, owner, dim, momentum=0.9, eps=1e-5,
                 use_scalar_scale=False):
        self.fix_ownership(owner)

        super().__init__()
        self.momentum = momentum
        self.eps = eps
        dim_scale = 1 if use_scalar_scale else dim
        self.register_buffer(
            'running_var', torch.zeros(dim_scale, dtype=torch.float32))
        self.register_buffer(
            'running_mean', torch.zeros(dim, dtype=torch.float32))
        self.weight = nn.Parameter(torch.ones(dim_scale, dtype=torch.float32))
        self.bias = nn.Parameter(torch.zeros(dim, dtype=torch.float32))
        self.use_scalar_scale = use_scalar_scale

    def fix_ownership(self, owner):
        # bypass pytorch hooks
        self.__dict__['owner'] = weakref.proxy(owner)

    def __repr__(self):
        return (f'{type(self).__name__}('
                f'{self.output_dim}, scalar={self.use_scalar_scale}, '
                f'cbd={self.bias_regularizer_coeff})')

    def forward(self, inp: torch.Tensor):
        if type(inp) is MultiSampleTensor:
            return inp.apply_batch(self.forward)

        if inp.ndim == 2:
            reduce_axes = [0]
            def view(x):
                return x.view(1, x.size(0))
        else:
            assert inp.ndim == 4
            reduce_axes = [0, 2, 3]
            def view(x):
                return x.view(1, x.size(0), 1, 1)

        if type(inp) is AbstractTensor:
            scale, bias = self._get_scale_bias(
                self.running_var, self.running_mean)
            scale = view(scale)
            bias = view(bias)
            return inp.apply_linear(scale, lambda x, w: x * w + bias)


        if self.training or (not self.training and self.owner.eval_with_bn):
            if self.use_scalar_scale:
                var = inp.flatten().var(unbiased=True)
            else:
                var = inp.var(reduce_axes, unbiased=True)
            mean = inp.mean(reduce_axes)
            if not self.owner.eval_with_bn:
                with torch.no_grad():
                    (self.running_var.
                     mul_(self.momentum).
                     add_(var * (1 - self.momentum)))
                    (self.running_mean.
                     mul_(self.momentum).
                     add_(mean * (1 - self.momentum)))
        else:
            var = self.running_var
            mean = self.running_mean

        #print(var, mean)


        scale, bias = self._get_scale_bias(var, mean)
        #print(scale, bias)
        ret = inp * view(scale) + view(bias)
        #print(ret)
        self.owner.on_bn_internals(self, scale, bias)
        #print(ret)
        #print(ok)
        return ret

    def _get_scale_bias(self, var, mean):
        std = torch.sqrt(var + self.eps)
        scale = self.weight / std
        bias = self.bias - mean * scale
        return scale, bias

    def get_scale_bias_eval(self):
        """get scale and bias using computed statistics"""
        return self._get_scale_bias(self.running_var, self.running_mean)

    @property
    def temperature(self):
        assert self.use_scalar_scale
        return self.weight.item()

    @property
    def output_dim(self):
        return self.running_mean.size(0)





def setattr_inplace(obj, k, v):
    assert hasattr(obj, k)
    setattr(obj, k, v)
    return obj

class SeqBinModelHelper:
    eval_with_bn = False
    """perform batch normalization even in eval mode: normalize the minibatch
    without updating running mean/var stats"""

    def eval_with_bn_(self, flag: bool):
        """inplace setter of the :attr:`eval_with_bn` flag"""
        self.eval_with_bn = bool(flag)
        return self

    def get_bn_state(self):
        """copy BN states into a list"""
        ret = []
        for i in self.features:
            if isinstance(i, BatchNormStatsCallbak):
                ret.append((
                    i.momentum, i.running_mean.clone(), i.running_var.clone()
                ))
        return ret

    def restore_bn_state(self, state):
        """restore states of BN from a list; return self"""
        idx = 0
        for i in self.features:
            if isinstance(i, BatchNormStatsCallbak):
                mom, mean, var = state[idx]
                idx += 1
                i.momentum = mom
                i.running_mean.copy_(mean)
                i.running_var.copy_(var)
        assert idx == len(state)
        return self

    def forward(self, x):
        if type(x) is AbstractTensor:
            assert isinstance(self.features[-3], Binarize01Act)
            return self.features[:-2](x)

        return self.features(x)

    def forward_with_multi_sample(
            self, x: torch.Tensor, x_adv: torch.Tensor,
            eps: float,
            inputs_min: float = 0, inputs_max: float = 1):
        """forward with randomly sampled perturbations and compute a
        stabilization loss """
        data = [x_adv, None, None]
        eps = float(eps)
        with torch.no_grad():
            delta = torch.empty_like(x).random_(0, 2).mul_(2*eps).sub_(eps)
            data[1] = torch.clamp_min_(x - delta, inputs_min)
            data[2] = torch.clamp_max_(x + delta, inputs_max)
            data = torch.cat([i[np.newaxis] for i in data], dim=0)
        y = self.forward(MultiSampleTensor.from_squeeze(data))
        return y.as_expanded_tensor()[0], y.loss

    def compute_act_stabilizing_loss_abstract(
            self, inputs: torch.Tensor, eps: float,
            inputs_min: float = 0, inputs_max: float = 1):
        """compute an extra loss for stabilizing the activations using abstract
            interpretation

        :return: loss value
        """
        loss = torch.tensor(0, dtype=torch.float32, device=inputs.device)
        with torch.no_grad():
            imin = torch.clamp_min_(inputs - eps, inputs_min)
            imax = torch.clamp_max_(inputs + eps, inputs_max)
        return self.forward(AbstractTensor(imin, imax, loss)).loss

    #def cvt_to_eval(self):
    #    return cvt_to_eval_sequential(self.features)

    @property
    def temperature(self):
        return self.features[-1].temperature

    def on_bn_internals(self, bn, scale, bias):
        pass

    def get_sparsity_stat(self):
        """:return: list of layer sparsity, total number of zeros, total number
        of weights"""
        nr_zero = 0
        tot = 0
        parts = []
        for name, parameter in self.features.named_parameters():
            #print(name, parameter)
            #for i in self.features:
            #print(i)
            #if not isinstance(i, (nn.Conv2d, Binarize01Act, nn.AvgPool2d,
            #                      nn.Flatten, nn.Linear, nn.BatchNorm2d)):
                #if isinstance(i[0][0], (BinConv2d, BinLinear)):
            #    for k in i:
            #        print(k)
            #        if not isinstance(k, (Binarize01Act)):
            #            for j in k:
            #                print(j)
            if "weight_mask" in name: #isinstance(name, (BinConv2d, BinLinear)):
                with torch.no_grad():
                    wb = parameter
                    nz = int((wb.abs() < 1e-4).to(dtype=torch.int32).sum())
                    n1 = int((wb.abs() > 1e-4).to(dtype=torch.int32).sum(dim= (1,2,3)).max())
                    cur_nr = wb.numel()
                    print(n1, parameter.shape)
                tot += cur_nr
                nr_zero += nz
                parts.append(nz / cur_nr)
        return parts, nr_zero, tot

    def _on_load_from_file(self):
        for i in self.features:
            if isinstance(i, BatchNormStatsCallbak):
                i.fix_ownership(self)


class BiasRegularizer:
    """a contextmanager to be applied on a network that can encourage small
    biases (a.k.a. cardinality bound decay)"""

    _loss = 0
    _loss_max = 0
    _num_items = 0
    _device = None
    _bn_prev = None

    consider_sparsity = False

    def __init__(self, coeff: float, thresh: float, net: SeqBinModelHelper):
        self.coeff = coeff
        self.thresh = thresh
        self.net = net
        self._bn_prev = {}

        # find previous layers of all BNs
        prev = None
        for i in net.features:
            if isinstance(i, BatchNormStatsCallbak):
                assert isinstance(prev, (BinConv2d, BinLinear))
                self._bn_prev[i] = prev
            prev = i

    def on_bn_internals(self, bn, scale, bias):
        coeff = bn.bias_regularizer_coeff
        if coeff == 0:
            return

        cur = torch.relu(-bias / scale - self.thresh)
        if self.consider_sparsity:
            with torch.no_grad():
                weight = self._bn_prev[bn].weight_bin
                dim = bn.output_dim
                assert dim == weight.size(0)
                weight = (weight.view(dim, -1).abs() > 1e-4).to(weight.dtype)
                weight = weight.sum(dim=1).detach_()

            assert cur.size() == weight.size(), (cur.size(), weight.size())
            cur = cur * weight

        this_loss = cur.sum()
        self._num_items += cur.numel()
        if coeff != 1:
            this_loss *= coeff
        self._loss += this_loss
        with torch.no_grad():
            self._loss_max = torch.max(self._loss_max, cur.max()).detach_()


    def __enter__(self):
        if self._device is None:
            self._device = next(iter(self.net.parameters())).device
        self._loss = torch.tensor(0, dtype=torch.float32, device=self._device)
        self._loss_max = torch.tensor(0, dtype=torch.float32,
                                      device=self._device)
        self._num_items = 0
        if self.coeff:
            self.net.on_bn_internals = self.on_bn_internals

    def __exit__(self, exc_type, exc_value, traceback):
        if self.coeff:
            del self.net.on_bn_internals
            self._loss *= self.coeff

    @property
    def loss(self):
        return self._loss

    @property
    def loss_avg(self):
        assert self._num_items
        return self._loss / self._num_items

    @property
    def loss_max(self):
        return self._loss_max


class InputQuantizer(nn.Module):
    """quantize input in the range ``[0, 1]`` to be a multiple of given ``step``
    """

    class RoundFn(Function):
        @staticmethod
        def forward(ctx, inp):
            return torch.round(inp)

        @staticmethod
        def backward(ctx, g_out):
            return g_out


    def __init__(self, step: float):
        super().__init__()
        self.step = step

    def forward(self, x):
        if type(x) is AbstractTensor:
            return AbstractTensor(self.forward(x.vmin), self.forward(x.vmax),
                                  x.loss)

        if type(x) is MultiSampleTensor:
            return x.apply_batch(self.forward)

        xint = self.RoundFn.apply(x / self.step)
        return xint * self.step

    def __repr__(self):
        return f'{type(self).__name__}({self.step})'


class model_cifar10lownoise(SeqBinModelHelper, nn.Module, ModelHelper):
    """see https://github.com/locuslab/convex_adversarial/blob/b3f532865cc02aeb6c020b449c2b1f0c13c7f236/examples/problems.py#L92"""

    CLASS2NAME = tuple(map(str, range(10)))

    def __init__(self, quant_step: float, args):
        super().__init__()
        self._setup_network(float(quant_step), args)
        self.args = args
        self.feature_pos = 10


    def _setup_network(self, quant_step, args):
        self.make_small_network(self, quant_step, args)

    @classmethod
    def make_small_network(
            cls, self, quant_step,args):

        nclass = 10

        if args.dataset == "Tiny":
            nclass = 200

        lin = BinLinearPos
        act = Binarize01Act
        wb3 = g_weight_binarizer3
        wb = g_weight_binarizer
        act3 = activation_quantize_fn2


        self.features = nn.Sequential(
        InputQuantizer(quant_step),  # 0
        nn.BatchNorm2d(args.nchannel),  # 1
        act3(),  # 2
        BinConv2d(wb3, 3, 48, 3, stride=2, padding=0,
                  rounding=False),
        act(),
        nn.Conv2d(48, 48*60, 3,
                  stride=2, padding=0,
                  groups=48),
        nn.BatchNorm2d(48*60),
        nn.ReLU(),
        nn.Conv2d(48*60, 48, 1, stride=1, padding=0,
                  groups=48),
        nn.BatchNorm2d(48),
        act(),
        Flatten(),
        lin(wb, 2352, nclass),
        setattr_inplace(
            BatchNormStatsCallbak(
                self, nclass,
                use_scalar_scale=g_use_scalar_scale_last_layer),
            'bias_regularizer_coeff', 0)
        )





    @classmethod
    def make_dataset_loader(cls, args, train: bool):
        if args.dataset == "MNIST":
            dataset = torchvision.datasets.MNIST(
            root=args.data, train=train, download=True,
            transform=transforms.Compose([
                transforms.ToTensor(),
            ]))
        elif args.dataset == "CIFAR10":
            if args.nchannel == 1:

                dataset = torchvision.datasets.CIFAR10(
                root=args.data, train=train, download=True,
                transform=transforms.Compose([
                    transforms.Grayscale(num_output_channels=1),
                    transforms.ToTensor(),
                ]))
            elif args.nchannel == 6:
                dataset = torchvision.datasets.CIFAR10(
                    root=args.data, train=train, download=True,
                    transform=transforms.Compose([
                        transforms.ToTensor(),
                    ]))
            else:
                dataset = torchvision.datasets.CIFAR10(
                root=args.data, train=train, download=True,
                transform=transforms.Compose([
                    transforms.ToTensor(),
            ]))
        elif args.dataset == "Tiny":
            if train:
                train_str = "train"
            else:
                train_str = "val"

            data_dir = args.data + '/tiny-imagenet-200/'
            if args.nchannel == 1:
                data_transforms = {
                    'train': transforms.Compose([
                        transforms.Grayscale(num_output_channels=1),
                        transforms.RandomRotation(20),
                        transforms.RandomHorizontalFlip(0.5),
                        transforms.ToTensor(),
                        #transforms.Normalize([0.4802, 0.4481, 0.3975], [0.2302, 0.2265, 0.2262]),
                    ]),
                    'val': transforms.Compose([
                        transforms.Grayscale(num_output_channels=1),
                        transforms.ToTensor(),
                        #transforms.Normalize([0.4802, 0.4481, 0.3975], [0.2302, 0.2265, 0.2262]),
                    ]),
                    'test': transforms.Compose([
                        transforms.Grayscale(num_output_channels=1),
                        transforms.ToTensor(),
                        #transforms.Normalize([0.4802, 0.4481, 0.3975], [0.2302, 0.2265, 0.2262]),
                    ])
                }
            elif args.nchannel == 6:
                data_transforms = {
                    'train': transforms.Compose([
                        transforms.RandomRotation(20),
                        transforms.RandomHorizontalFlip(0.5),
                        transforms.ToTensor(),
                        #transforms.Normalize([0.4802, 0.4481, 0.3975], [0.2302, 0.2265, 0.2262]),
                    ]),
                    'val': transforms.Compose([
                        transforms.ToTensor(),
                        #transforms.Normalize([0.4802, 0.4481, 0.3975], [0.2302, 0.2265, 0.2262]),
                    ]),
                    'test': transforms.Compose([
                        transforms.ToTensor(),
                        #transforms.Normalize([0.4802, 0.4481, 0.3975], [0.2302, 0.2265, 0.2262]),
                    ])
                }
            else:
                data_transforms = {
                    'train': transforms.Compose([
                        transforms.RandomRotation(20),
                        transforms.RandomHorizontalFlip(0.5),
                        transforms.ToTensor(),
                        #transforms.Normalize([0.4802, 0.4481, 0.3975], [0.2302, 0.2265, 0.2262]),
                    ]),
                    'val': transforms.Compose([
                        transforms.ToTensor(),
                        #transforms.Normalize([0.4802, 0.4481, 0.3975], [0.2302, 0.2265, 0.2262]),
                    ]),
                    'test': transforms.Compose([
                        transforms.ToTensor(),
                        #transforms.Normalize([0.4802, 0.4481, 0.3975], [0.2302, 0.2265, 0.2262]),
                    ])
                }

            dataset = torchvision.datasets.ImageFolder(os.path.join(data_dir, train_str), data_transforms[train_str])

        else:
            raise "PB"

        loader = torch.utils.data.DataLoader(
            dataset, batch_size=args.batchsize, shuffle=train,
            num_workers=args.workers if train else 0)


        return loader
