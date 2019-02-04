import math

import numpy as np
import torch
import torch.nn.functional as F
from padertorch.base import Module
from padertorch.ops.mappings import ACTIVATION_FN_MAP
from padertorch.utils import to_list
from torch import nn


class Pad(Module):
    def __init__(
            self, kernel_size, stride=1, side='both', mode='constant'
    ):
        super().__init__()
        self.kernel_size = kernel_size
        self.stride = stride
        self.side = side
        self.mode = mode

    def forward(self, x):
        """
        expects time axis to be on the last dim
        :param x:
        :return:
        """
        if self.stride is None:
            return x
        k = self.kernel_size - 1
        if k > 0:
            tail = (x.shape[-1] - 1) % self.stride
            if self.side == 'front':
                pad = (k - tail, 0)
            elif self.side == 'both':
                pad = ((k - tail) // 2, math.ceil((k - tail) / 2))
            elif self.side == 'end':
                pad = (0, k - tail)
            else:
                raise ValueError
            x = F.pad(x, pad, mode=self.mode)
        return x


class Cut(Module):
    def __init__(self, side='both'):
        super().__init__()
        self.side = side

    def forward(self, x, size):
        if self.side is None:
            assert size == 0
            return x
        if size > 0:
            if self.side == 'front':
                x = x[..., size:]
            elif self.side == 'both':
                x = x[..., size//2: -math.ceil(size / 2)]
            elif self.side == 'end':
                x = x[..., :-size]
            else:
                raise ValueError
        return x


class Scale(Module):
    """
    >>> print(Scale()(torch.Tensor(np.arange(10)).view(1,1,10), 5))
    tensor([[[0.5000, 2.5000, 4.5000, 6.5000, 8.5000]]])
    >>> print(Scale(padding='front')(torch.Tensor(np.arange(10)).view(1,1,10), 3))
    tensor([[[0.2500, 3.5000, 7.5000]]])
    >>> print(Scale(padding='both')(torch.Tensor(np.arange(10)).view(1,1,10), 3))
    tensor([[[0.7500, 4.5000, 8.2500]]])
    >>> print(Scale(padding='end')(torch.Tensor(np.arange(10)).view(1,1,10), 3))
    tensor([[[1.5000, 5.5000, 8.7500]]])
    >>> print(Scale(padding=None)(torch.Tensor(np.arange(10)).view(1,1,10), 6))
    tensor([[[4., 5., 6., 7., 8., 9.]]])
    """
    def __init__(self, padding='both'):
        super().__init__()
        self.padding = padding

    def forward(self, x, size):
        if size == 1:
            return x.mean(dim=-1, keepdim=True)
        if self.padding is None:
            stride = x.shape[-1] // size
        else:
            stride = int(np.ceil((x.shape[-1] - 1) / (size - 1e-10)))
            assert stride <= (x.shape[-1] - 1) / (size - 1)
            x = Pad(
                kernel_size=stride, stride=stride,
                side=self.padding, mode="replicate"
            )(x)
        if 1 < stride:
            x = F.avg_pool1d(x, stride)
        if x.shape[-1] > size:
            # Cut front because no padding was used
            # Is this the right behavior?
            x = Cut(side='front')(x, x.shape[-1] - size)
        if x.shape[-1] < size:
            x = F.interpolate(x, size, mode='linear')
        assert x.shape[-1] == size
        return x


class MaxPool1d(Module):
    def __init__(self, kernel_size, padding='both'):
        super().__init__()
        self.kernel_size = kernel_size
        self.padding = padding

    def forward(self, x):
        if self.kernel_size < 2:
            return x, None
        x = Pad(
            kernel_size=self.kernel_size,
            stride=self.kernel_size,
            side=self.padding
        )(x)
        return nn.MaxPool1d(
            kernel_size=self.kernel_size, return_indices=True
        )(x)


class MaxUnpool1d(Module):
    def __init__(self, kernel_size, padding='both'):
        super().__init__()
        self.kernel_size = kernel_size
        self.padding = padding

    def forward(self, x, indices):
        if self.kernel_size < 2:
            return x
        x = Cut(side=self.padding)(x, size=(x.shape[-1] - indices.shape[-1]))
        return nn.MaxUnpool1d(kernel_size=self.kernel_size)(x, indices=indices)


class Conv1d(Module):
    def __init__(
            self, input_size, output_size, condition_size=0, kernel_size=5,
            dilation=1, stride=1, transpose=False, padding='both', bias=True,
            groups=1, norm=None, dropout=0., activation='leaky_relu',
            gated=False
    ):
        super().__init__()
        self.kernel_size = kernel_size
        self.dilation = dilation
        self.stride = stride
        self.transpose = transpose
        self.padding = padding
        self.dropout = dropout
        self.activation = ACTIVATION_FN_MAP[activation]()
        self.gated = gated

        conv_cls = nn.ConvTranspose1d if transpose else nn.Conv1d
        self.conv = conv_cls(
            input_size + condition_size, output_size,
            kernel_size=kernel_size, dilation=dilation, stride=stride,
            bias=bias, groups=groups)
        torch.nn.init.xavier_uniform_(self.conv.weight)
        if bias:
            torch.nn.init.zeros_(self.conv.bias)
        if norm is None:
            self.norm = None
        elif norm == 'batch':
            self.norm = nn.BatchNorm1d(output_size)
        else:
            raise ValueError(f'{norm} normalization  not known.')
        if self.gated:
            self.gate_conv = conv_cls(
                input_size + condition_size, output_size,
                kernel_size=kernel_size, dilation=dilation, stride=stride,
                bias=bias, groups=groups)
            torch.nn.init.xavier_uniform_(self.gate_conv.weight)
            if bias:
                torch.nn.init.zeros_(self.gate_conv.bias)

    def forward(self, x, h=None):
        if self.training and self.dropout > 0.:
            x = F.dropout(x, self.dropout)

        if h is not None:
            x = torch.cat(
                (x, Scale(padding=self.padding)(h, x.shape[-1])), dim=1)

        if not self.transpose:
            x = Pad(
                kernel_size=1 + self.dilation * (self.kernel_size - 1),
                stride=self.stride,
                side=self.padding
            )(x)

        y = self.conv(x)
        if self.norm is not None:
            y = self.norm(y)
        y = self.activation(y)

        if self.gated:
            g = self.gate_conv(x)
            y = y * torch.sigmoid(g)

        if self.transpose:
            k = 1 + self.dilation * (self.kernel_size - 1)
            y = Cut(side=self.padding)(y, size=k - self.stride)

        return y


class MultiScaleConv1d(Module):
    def __init__(
            self, input_size, hidden_size, output_size, condition_size=0,
            kernel_size=2, n_scales=1, dilation=False, stride=1,
            transpose=False, padding='both', norm=None, dropout=0.,
            activation='leaky_relu', gated=False
    ):
        assert hidden_size % n_scales == 0, (hidden_size, n_scales)
        super().__init__()
        if dilation is True or dilation == 1:
            kernel_sizes = n_scales * [kernel_size]
            dilations = [2 ** i for i in range(n_scales)]
        elif dilation is False or dilation == 0:
            kernel_sizes = [
                1 + (kernel_size - 1) * 2**i for i in range(n_scales)
            ]
            dilations = n_scales * [1]
        else:
            raise ValueError('dilation not a boolean.')
        self.convs = nn.ModuleList([
            Conv1d(
                input_size=input_size, output_size=hidden_size // n_scales,
                condition_size=condition_size, kernel_size=kernel_sizes[i],
                dilation=dilations[i], stride=stride, transpose=transpose,
                padding=padding, norm=None, dropout=dropout,
                activation=activation, gated=gated
            )
            for i in range(n_scales)
        ])
        self.out = Conv1d(
            input_size=hidden_size, output_size=output_size, kernel_size=1,
            activation='identity', norm=None
        )

        if norm is None:
            self.norm = None
        elif norm == 'batch':
            self.norm = nn.BatchNorm1d(output_size)
        else:
            raise ValueError(f'{norm} normalization not known.')

    def forward(self, x, h=None):
        y = self.out(torch.cat([conv(x, h) for conv in self.convs], dim=1))
        if y.shape == x.shape:
            y = y + x
        if self.norm is not None:
            y = self.norm(y)
        return y


class TCN(Module):
    """
    Multi-Scale Temporal Convolutional Network
    """
    def __init__(
            self, input_size, output_size, hidden_sizes=256, condition_size=0,
            num_layers=5, kernel_sizes=3, n_scales=None, dilations=1, strides=1,
            transpose=False, pool_sizes=1, padding='both', norm=None,
            dropout=0., activation='leaky_relu', gated=False
    ):
        super().__init__()

        self.input_size = input_size
        self.hidden_sizes = to_list(
            hidden_sizes, num_layers - int(n_scales is None)
        )
        self.output_size = output_size
        self.condition_size = condition_size
        self.num_layers = num_layers
        self.kernel_sizes = to_list(kernel_sizes, num_layers)
        self.n_scales = None if n_scales is None else to_list(
            n_scales, num_layers)
        self.dilations = to_list(dilations, num_layers)
        self.strides = to_list(strides, num_layers)
        self.pool_sizes = to_list(pool_sizes, num_layers)
        self.transpose = transpose
        self.padding = padding

        convs = list()
        for i in range(num_layers):
            if n_scales is None:
                if i == num_layers - 1:
                    output_size_ = output_size
                    norm = None
                    activation = 'identity'
                else:
                    output_size_ = self.hidden_sizes[i]
                convs.append(Conv1d(
                    input_size=input_size, output_size=output_size_,
                    condition_size=condition_size,
                    kernel_size=self.kernel_sizes[i],
                    dilation=self.dilations[i],
                    stride=self.strides[i], transpose=transpose,
                    padding=padding, norm=norm, dropout=dropout,
                    activation=activation, gated=gated
                ))
            else:
                hidden_size = self.hidden_sizes[i]
                if i == num_layers - 1:
                    output_size_ = output_size
                    norm = None
                else:
                    output_size_ = hidden_size
                convs.append(MultiScaleConv1d(
                    input_size=input_size, hidden_size=hidden_size,
                    output_size=output_size_, condition_size=condition_size,
                    kernel_size=self.kernel_sizes[i], n_scales=self.n_scales[i],
                    dilation=self.dilations[i], stride=self.strides[i],
                    transpose=transpose, padding=padding, norm=norm,
                    dropout=dropout, activation=activation, gated=gated
                ))
            input_size = output_size_
        self.convs = nn.ModuleList(convs)

    def forward(self, x, h=None, pool_indices=None):
        pool_indices = to_list(pool_indices, self.num_layers)
        for i, conv in enumerate(self.convs):
            pool_size = self.pool_sizes[i]
            if self.transpose:
                pool = MaxUnpool1d(kernel_size=pool_size, padding=self.padding)
                x = pool(x, indices=pool_indices[i])
            x = conv(x, h)
            if not self.transpose:
                pool = MaxPool1d(kernel_size=pool_size, padding=self.padding)
                x, pool_indices[i] = pool(x)
        if self.transpose:
            return x
        return x, pool_indices
