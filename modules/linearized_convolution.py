import torch
import torch.nn as nn
import torch.nn.functional as F


class LinearizedConvolution(nn.Conv1d):
    """An optimized version of nn.Conv1d.

    This module replaces convolutions with linear layers as appropriate
    and supports optimizations for incremental inference.
    """

    def __init__(self, in_channels, out_channels, kernel_size, **kwargs):
        super().__init__(in_channels, out_channels, kernel_size, **kwargs)
        self.clear_buffer()

        self._linearized_weight = None
        self.register_backward_hook(self._clear_linearized_weight)

    def forward(self, x):
        if self.kernel_size[0] > 1:
            x = x.transpose(1, 2)  # B x T x C -> B x C x T
            x = super().forward(x)
            x = x.transpose(2, 1)  # B x C x T -> B x T x C
        else:
            x = F.linear(x, self.weight.squeeze(2), self.bias)
        return x

    def remove_future_timesteps(self, x):
        """Remove future time steps created by padding."""
        if self.kernel_size[0] > 1 and self.padding[0] > 0:
            x = x[:, :-self.padding[0], :]
        return x

    def incremental_forward(self, input):
        """Forward convolution one time step at a time.

        This function maintains an internal state to buffer signal and
        accepts a single frame as input. If the input order changes
        between time steps, call reorder_buffer. To apply to fresh
        inputs, call clear_buffer.
        """
        if self.training:
            raise RuntimeError('LinearizedConvolution only supports inference')

        # run forward pre hooks (e.g., weight norm)
        for hook in self._forward_pre_hooks.values():
            hook(self, input)

        # reshape weight
        weight = self._get_linearized_weight()
        kw = self.kernel_size[0]

        bsz = input.size(0)  # input: bsz x len x dim
        if kw > 1:
            input = input.data
            if self.input_buffer is None:
                self.input_buffer = input.new(bsz, kw, input.size(2))
                self.input_buffer.zero_()
            else:
                # shift buffer
                self.input_buffer[:, :-1, :] = self.input_buffer[:, 1:, :].clone()
            # append next input
            self.input_buffer[:, -1, :] = input[:, -1, :]
            input = torch.autograd.Variable(self.input_buffer, volatile=True)
        output = F.linear(input.view(bsz, -1), weight, self.bias)
        return output.view(bsz, 1, -1)

    def clear_buffer(self):
        self.input_buffer = None

    def reorder_buffer(self, new_order):
        if self.input_buffer is not None:
            self.input_buffer = self.input_buffer.index_select(0, new_order)

    def _get_linearized_weight(self):
        if self._linearized_weight is None:
            nout, nin, kw = self.weight.size()
            self._linearized_weight = self.weight \
                .transpose(1, 2).contiguous() \
                .view(nout, kw * nin)
        return self._linearized_weight

    def _clear_linearized_weight(self, *args):
        self._linearized_weight = None
