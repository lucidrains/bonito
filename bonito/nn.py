"""
Bonito nn modules.
"""

import torch
from torch import nn
from torch.nn import Module
from torch.nn.init import orthogonal_


layers = {}


def register(layer):
    layer.name = layer.__name__.lower()
    layers[layer.name] = layer
    return layer


register(torch.nn.ReLU)
register(torch.nn.Tanh)


@register
class Swish(torch.nn.SiLU):
    pass


@register
class Serial(torch.nn.Sequential):

    def __init__(self, sublayers):
        super().__init__(*sublayers)

    def to_dict(self, include_weights=False):
        return {
            'sublayers': [to_dict(layer, include_weights) for layer in self._modules.values()]
        }


@register
class Reverse(Module):

    def __init__(self, sublayers):
        super().__init__()
        self.layer = Serial(sublayers) if isinstance(sublayers, list) else sublayers

    def forward(self, x):
        return self.layer(x.flip(0)).flip(0)

    def to_dict(self, include_weights=False):
        if isinstance(self.layer, Serial):
            return self.layer.to_dict(include_weights)
        else:
            return {'sublayers': to_dict(self.layer, include_weights)}


@register
class Convolution(Module):

    def __init__(self, insize, size, winlen, stride=1, padding=0, bias=True, activation=None):
        super().__init__()
        self.conv = torch.nn.Conv1d(insize, size, winlen, stride=stride, padding=padding, bias=bias)
        self.activation = layers.get(activation, lambda: activation)()

    def forward(self, x):
        if self.activation is not None:
            return self.activation(self.conv(x))
        return self.conv(x)

    def to_dict(self, include_weights=False):
        res = {
            "insize": self.conv.in_channels,
            "size": self.conv.out_channels,
            "bias": self.conv.bias is not None,
            "winlen": self.conv.kernel_size[0],
            "stride": self.conv.stride[0],
            "padding": self.conv.padding[0],
            "activation": self.activation.name if self.activation else None,
        }
        if include_weights:
            res['params'] = {
                'W': self.conv.weight, 'b': self.conv.bias if self.conv.bias is not None else []
            }
        return res


@register
class LinearCRFEncoder(Module):

    def __init__(self, insize, n_base, state_len, bias=True, scale=None, activation=None, blank_score=None):
        super().__init__()
        self.n_base = n_base
        self.state_len = state_len
        self.blank_score = blank_score
        size = (n_base + 1) * n_base**state_len if blank_score is None else n_base**(state_len + 1)
        self.linear = torch.nn.Linear(insize, size, bias=bias)
        self.activation = layers.get(activation, lambda: activation)()
        self.scale = scale

    def forward(self, x):
        scores = self.linear(x)
        if self.activation is not None:
            scores = self.activation(scores)
        if self.scale is not None:
            scores = scores * self.scale
        if self.blank_score is not None:
            T, N, C = scores.shape
            s = torch.tensor(self.blank_score, device=scores.device, dtype=scores.dtype)
            scores = torch.cat([s.expand(T, N, C//self.n_base, 1), scores.reshape(T, N, C//self.n_base, self.n_base)], axis=-1).reshape(T, N, -1)
        return scores

    def to_dict(self, include_weights=False):
        res = {
            'insize': self.linear.in_features,
            'n_base': self.n_base,
            'state_len': self.state_len,
            'bias': self.linear.bias is not None,
            'scale': self.scale,
            'activation': self.activation.name if self.activation else None,
            'blank_score': self.blank_score,
        }
        if include_weights:
            res['params'] = {
                'W': self.linear.weight, 'b': self.linear.bias
                if self.linear.bias is not None else []
            }
        return res


@register
class SHA(Module):

    def __init__(self, dim, dropout=0.):
        super().__init__()
        self.scale = dim ** -0.5
        self.to_q = nn.Sequential(nn.Linear(dim, dim), nn.LayerNorm(dim))
        self.dropout = nn.Dropout(dropout)
        self.layerscale = LayerScale(dim)

    def forward(self, x, kv):
        x = x.transpose(0, 1)
        kv = kv.transpose(0, 1)

        q = self.to_q(x)
        sim = torch.matmul(q, kv.transpose(-1, -2)) * self.scale
        attn = sim.softmax(dim=-1)
        attn = self.dropout(attn)

        out = torch.matmul(attn, kv)
        out = out.transpose(0, 1)
        return self.layerscale(out)

@register
class MHA(Module):

    def __init__(self, dim, heads=4, dim_head=64, dropout=0.):
        super().__init__()
        inner_dim = heads * dim_head
        self.heads = heads
        self.dim_head = dim_head
        self.scale = dim_head ** -0.5
        self.to_q = nn.Linear(dim, inner_dim, bias=False)
        self.norm_q = nn.LayerNorm(dim_head)
        self.to_kv = nn.Linear(dim, inner_dim, bias=False)
        self.to_out = nn.Sequential(nn.Linear(inner_dim, dim), nn.LayerNorm(dim))
        self.dropout = nn.Dropout(dropout)
        self.layerscale = LayerScale(dim)

    def forward(self, x, kv):
        n, b, d, h = *x.shape, self.heads

        x = x.transpose(0, 1)
        kv = kv.transpose(0, 1)

        q = self.to_q(x)
        kv = self.to_kv(kv)

        q, kv = map(lambda t: t.reshape(b, -1, h, self.dim_head).transpose(1, 2), (q, kv))
        q = self.norm_q(q)

        sim = torch.matmul(q, kv.transpose(-1, -2)) * self.scale
        attn = sim.softmax(dim=-1)
        attn = self.dropout(attn)

        out = torch.matmul(attn, kv)
        out = out.transpose(1, 2).reshape(b, n, -1)
        out = self.to_out(out)

        out = out.transpose(0, 1)
        return self.layerscale(out)

@register
class LayerScale(Module):
    """ https://arxiv.org/abs/2103.17239 """

    def __init__(self, features, eps=1e-5):
        super().__init__()
        scale = torch.zeros(1, 1, features).fill_(eps)
        self.scale = nn.Parameter(scale)

    def forward(self, x):
        return self.scale * x

@register
class FeedForward(Module):

    def __init__(self, dim, mult=4, dropout=0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, dim * mult),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(dim * mult, dim),
            LayerScale(dim)
        )

    def forward(self, x):
        return self.net(x)

@register
class Boom(Module):

    def __init__(self, dim, mult=4, dropout=0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, dim * mult),
            nn.GELU(),
            nn.Dropout(dropout)
        )
        self.layerscale = LayerScale(dim)

    def forward(self, x):
        *precede_dims, dim = x.shape
        out = self.net(x)
        out = out.reshape(*precede_dims, -1, dim)
        out = out.sum(dim = -2)
        return self.layerscale(out)

@register
class SHABlock(Module):
    """ https://arxiv.org/abs/1911.11423 """

    def __init__(self, dim, attn_dropout=0., ff_dropout=0., num_attn_heads=1, ff_mult=4, boom=False):
        super().__init__()
        self.attn_query_norm = nn.LayerNorm(dim)
        self.attn_kv_norm = nn.LayerNorm(dim)

        is_multiheaded = num_attn_heads > 1

        if is_multiheaded:
            self.attn = MHA(dim=dim, dropout=attn_dropout, heads=num_attn_heads)
        else:
            self.attn = SHA(dim=dim, dropout=attn_dropout)

        if boom:
            self.ff = Boom(dim=dim, dropout=ff_dropout, mult=ff_mult)
        else:
            self.ff = FeedForward(dim=dim, dropout=ff_dropout, mult=ff_mult)

    def forward(self, x):
        kv = self.attn_kv_norm(x)
        q = self.attn_query_norm(x)

        x = self.attn(q, kv) + x
        x = self.ff(x) + x
        return x

@register
class Permute(Module):

    def __init__(self, dims):
        super().__init__()
        self.dims = dims

    def forward(self, x):
        return x.permute(*self.dims)

    def to_dict(self, include_weights=False):
        return {'dims': self.dims}


def truncated_normal(size, dtype=torch.float32, device=None, num_resample=5):
    x = torch.empty(size + (num_resample,), dtype=torch.float32, device=device).normal_()
    i = ((x < 2) & (x > -2)).max(-1, keepdim=True)[1]
    return torch.clamp_(x.gather(-1, i).squeeze(-1), -2, 2)


class RNNWrapper(Module):
    def __init__(
            self, rnn_type, *args, reverse=False, orthogonal_weight_init=True, disable_state_bias=True, bidirectional=False, **kwargs
    ):
        super().__init__()
        if reverse and bidirectional:
            raise Exception("'reverse' and 'bidirectional' should not both be set to True")
        self.reverse = reverse
        self.rnn = rnn_type(*args, bidirectional=bidirectional, **kwargs)
        self.init_orthogonal(orthogonal_weight_init)
        self.init_biases()
        if disable_state_bias: self.disable_state_bias()

    def forward(self, x):
        if self.reverse: x = x.flip(0)
        y, h = self.rnn(x)
        if self.reverse: y = y.flip(0)
        return y

    def init_biases(self, types=('bias_ih',)):
        for name, param in self.rnn.named_parameters():
            if any(k in name for k in types):
                with torch.no_grad():
                    param.set_(0.5*truncated_normal(param.shape, dtype=param.dtype, device=param.device))

    def init_orthogonal(self, types=True):
        if not types: return
        if types == True: types = ('weight_ih', 'weight_hh')
        for name, x in self.rnn.named_parameters():
            if any(k in name for k in types):
                for i in range(0, x.size(0), self.rnn.hidden_size):
                    orthogonal_(x[i:i+self.rnn.hidden_size])

    def disable_state_bias(self):
        for name, x in self.rnn.named_parameters():
            if 'bias_hh' in name:
                x.requires_grad = False
                x.zero_()


@register
class LSTM(RNNWrapper):

    def __init__(self, size, insize, bias=True, reverse=False):
        super().__init__(torch.nn.LSTM, size, insize, bias=bias, reverse=reverse)

    def to_dict(self, include_weights=False):
        res = {
            'size': self.rnn.hidden_size,
            'insize': self.rnn.input_size,
            'bias': self.rnn.bias,
            'reverse': self.reverse,
        }
        if include_weights:
            res['params'] = {
                'iW': self.rnn.weight_ih_l0.reshape(4, self.rnn.hidden_size, self.rnn.input_size),
                'sW': self.rnn.weight_hh_l0.reshape(4, self.rnn.hidden_size, self.rnn.hidden_size),
                'b': self.rnn.bias_ih_l0.reshape(4, self.rnn.hidden_size)
            }
        return res


def to_dict(layer, include_weights=False):
    if hasattr(layer, 'to_dict'):
        return {'type': layer.name, **layer.to_dict(include_weights)}
    return {'type': layer.name}


def from_dict(model_dict, layer_types=None):
    model_dict = model_dict.copy()
    if layer_types is None:
        layer_types = layers
    type_name = model_dict.pop('type')
    typ = layer_types[type_name]
    if 'sublayers' in model_dict:
        sublayers = model_dict['sublayers']
        model_dict['sublayers'] = [
            from_dict(x, layer_types) for x in sublayers
        ] if isinstance(sublayers, list) else from_dict(sublayers, layer_types)
    try:
        layer = typ(**model_dict)
    except Exception as e:
        raise Exception(f'Failed to build layer of type {typ} with args {model_dict}') from e
    return layer
