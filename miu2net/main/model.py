from typing import Union, List
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.transforms.functional import resize


class ConvBNReLU(nn.Module):
    def __init__(self, in_ch: int, out_ch: int, kernel_size: int = 3, dilation: int = 1):
        super().__init__()

        padding = kernel_size // 2 if dilation == 1 else dilation
        self.conv = nn.Conv2d(in_ch, out_ch, kernel_size, padding=padding, dilation=dilation, bias=False)
        self.bn = nn.BatchNorm2d(out_ch)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.relu(self.bn(self.conv(x)))


class DownConvBNReLU(ConvBNReLU):
    def __init__(self, in_ch: int, out_ch: int, kernel_size: int = 3, dilation: int = 1, flag: bool = True):
        super().__init__(in_ch, out_ch, kernel_size, dilation)
        self.down_flag = flag

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.down_flag:
            x = F.avg_pool2d(x, kernel_size=2, stride=2, ceil_mode=True)

        return self.relu(self.bn(self.conv(x)))


class UpConvBNReLU(ConvBNReLU):
    def __init__(self, in_ch: int, out_ch: int, kernel_size: int = 3, dilation: int = 1, flag: bool = True):
        super().__init__(in_ch, out_ch, kernel_size, dilation)
        self.up_flag = flag

    def forward(self, x1: torch.Tensor, x2: torch.Tensor) -> torch.Tensor:
        if self.up_flag:
            x1 = F.interpolate(x1, size=x2.shape[2:], mode='bilinear', align_corners=False)
        return self.relu(self.bn(self.conv(torch.cat([x1, x2], dim=1))))


class RSU(nn.Module):
    def __init__(self, height: int, in_ch: int, mid_ch: int, out_ch: int):
        super().__init__()

        assert height >= 2
        self.conv_in = ConvBNReLU(in_ch, out_ch)

        encode_list = [DownConvBNReLU(out_ch, mid_ch, flag=False)]
        decode_list = [UpConvBNReLU(mid_ch * 2, mid_ch, flag=False)]
        for i in range(height - 2):
            encode_list.append(DownConvBNReLU(mid_ch, mid_ch))
            decode_list.append(UpConvBNReLU(mid_ch * 2, mid_ch if i < height - 3 else out_ch))

        encode_list.append(ConvBNReLU(mid_ch, mid_ch, dilation=2))
        self.encode_modules = nn.ModuleList(encode_list)
        self.decode_modules = nn.ModuleList(decode_list)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x_in = self.conv_in(x)

        x = x_in
        encode_outputs = []
        for m in self.encode_modules:
            x = m(x)
            encode_outputs.append(x)

        x = encode_outputs.pop()
        for m in self.decode_modules:
            x2 = encode_outputs.pop()
            x = m(x, x2)

        return x + x_in


class RSU4F(nn.Module):
    def __init__(self, in_ch: int, mid_ch: int, out_ch: int):
        super().__init__()
        self.conv_in = ConvBNReLU(in_ch, out_ch)
        self.encode_modules = nn.ModuleList([ConvBNReLU(out_ch, mid_ch),
                                             ConvBNReLU(mid_ch, mid_ch, dilation=2),
                                             ConvBNReLU(mid_ch, mid_ch, dilation=4),
                                             ConvBNReLU(mid_ch, mid_ch, dilation=8)])

        self.decode_modules = nn.ModuleList([ConvBNReLU(mid_ch * 2, mid_ch, dilation=4),
                                             ConvBNReLU(mid_ch * 2, mid_ch, dilation=2),
                                             ConvBNReLU(mid_ch * 2, out_ch)])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x_in = self.conv_in(x)

        x = x_in
        encode_outputs = []
        for m in self.encode_modules:
            x = m(x)
            encode_outputs.append(x)

        x = encode_outputs.pop()
        for m in self.decode_modules:
            x2 = encode_outputs.pop()
            x = m(torch.cat([x, x2], dim=1))

        return x + x_in


class U2Net(nn.Module):
    def __init__(self, cfg: dict, out_ch: int = 1, mode: str = '1x1conv'):
        super().__init__()
        assert mode in ['1x1conv', 'laplacian_pyr']
        self.mode = mode
        assert "encode" in cfg
        assert "decode" in cfg
        self.encode_num = len(cfg["encode"])

        encode_list = []
        side_list = []
        for c in cfg["encode"]:
            # c: [height, in_ch, mid_ch, out_ch, RSU4F, side]
            assert len(c) == 6
            encode_list.append(RSU(*c[:4]) if c[4] is False else RSU4F(*c[1:4]))

            if c[5] is True:
                side_list.append(nn.Conv2d(c[3], out_ch, kernel_size=3, padding=1))
        self.encode_modules = nn.ModuleList(encode_list)

        decode_list = []
        for c in cfg["decode"]:
            # c: [height, in_ch, mid_ch, out_ch, RSU4F, side]
            assert len(c) == 6
            decode_list.append(RSU(*c[:4]) if c[4] is False else RSU4F(*c[1:4]))

            if c[5] is True:
                side_list.append(nn.Conv2d(c[3], out_ch, kernel_size=3, padding=1))
        self.decode_modules = nn.ModuleList(decode_list)
        self.side_modules = nn.ModuleList(side_list)
        self.out_conv = nn.Conv2d(self.encode_num * out_ch, out_ch, kernel_size=1)
        init_1x1_w = torch.zeros([1, 6, 1, 1])
        torch.nn.init.constant_(init_1x1_w, val=1/6)
        init_1x1_b = torch.tensor([0.])
        with torch.no_grad():
            self.out_conv.weight = torch.nn.Parameter(init_1x1_w)
            self.out_conv.bias = torch.nn.Parameter(init_1x1_b)

    def forward(self, x: torch.Tensor) -> Union[torch.Tensor, List[torch.Tensor]]:
        _, _, h, w = x.shape

        # collect encode outputs
        encode_outputs = []
        for i, m in enumerate(self.encode_modules):
            x = m(x)
            encode_outputs.append(x)
            if i != self.encode_num - 1:
                x = F.avg_pool2d(x, kernel_size=2, stride=2, ceil_mode=True)

        # collect decode outputs
        x = encode_outputs.pop()
        decode_outputs = [x]
        for m in self.decode_modules:
            x2 = encode_outputs.pop()
            x = F.interpolate(x, size=x2.shape[2:], mode='bilinear', align_corners=False)
            x = m(torch.concat([x, x2], dim=1))
            decode_outputs.insert(0, x)

        if self.mode == '1x1conv':
            # collect side outputs
            side_outputs = []
            for m in self.side_modules:
                x = decode_outputs.pop()
                x = F.interpolate(m(x), size=[h, w], mode='bilinear', align_corners=False)
                side_outputs.insert(0, x)
            x = self.out_conv(torch.concat(side_outputs, dim=1))
            return [x] + side_outputs

        elif self.mode == 'laplacian_pyr':
            # collect side outputs (as a Laplacian pyramid)
            lap_pyramid = []
            for i, m in enumerate(self.side_modules):
                x = decode_outputs.pop()
                # height and width of the current Laplacian pyramid level
                pyr_h, pyr_w = h // 2**(5-i), w // 2**(5-i)
                x = F.interpolate(m(x), size=[pyr_h, pyr_w], mode='bilinear', align_corners=False)
                lap_pyramid.insert(0, x)

            # reconstruct original image resolution from Laplacian pyramid
            x = lap_pyramid[-1]
            for laplacian in reversed(lap_pyramid[:-1]):
                # Upsample the current image
                upsampled = resize(x, size=laplacian.shape[-2:])
                # Add the Laplacian layer
                x = upsampled + laplacian
            return [x] + lap_pyramid


def u2net_full(out_ch: int = 1, in_ch: int = 2, mode: str = '1x1conv'):
    cfg = {
        # height, in_ch, mid_ch, out_ch, RSU4F, side
        "encode": [[7, in_ch, 32, 64, False, False],      # En1
                   [6, 64, 32, 128, False, False],    # En2
                   [5, 128, 64, 256, False, False],   # En3
                   [4, 256, 128, 512, False, False],  # En4
                   [4, 512, 256, 512, True, False],   # En5
                   [4, 512, 256, 512, True, True]],   # En6
        # height, in_ch, mid_ch, out_ch, RSU4F, side
        "decode": [[4, 1024, 256, 512, True, True],   # De5
                   [4, 1024, 128, 256, False, True],  # De4
                   [5, 512, 64, 128, False, True],    # De3
                   [6, 256, 32, 64, False, True],     # De2
                   [7, 128, 16, 64, False, True]]     # De1
    }

    return U2Net(cfg, out_ch, mode=mode)



########################################################################
# DeepMass network translated into PyTorch
########################################################################

class UNetDeepMass(nn.Module):
    """
    Adapted from DeepMass UNet
    """

    def __init__(self, channels=[1, 1]):
        super(UNetDeepMass, self).__init__()
        self.channels = channels

        # Define the layers
        self.conv1 = nn.Conv2d(channels[0], 16, kernel_size=3, padding=1)
        self.bn1 = nn.BatchNorm2d(16)

        self.pool = nn.AvgPool2d(kernel_size=2, stride=2)

        self.conv2 = nn.Conv2d(16, 32, kernel_size=3, padding=1)
        self.bn2 = nn.BatchNorm2d(32)

        self.conv3 = nn.Conv2d(32, 64, kernel_size=3, padding=1)
        self.bn3 = nn.BatchNorm2d(64)

        self.conv4 = nn.Conv2d(64, 64, kernel_size=3, padding=1)
        self.bn4 = nn.BatchNorm2d(64)

        self.conv_deep = nn.Conv2d(64, 64, kernel_size=3, padding=1)
        self.bn_deep = nn.BatchNorm2d(64)

        self.upsample = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)

        self.conv_deep2 = nn.Conv2d(128, 64, kernel_size=3, padding=1)
        self.bn_deep2 = nn.BatchNorm2d(64)

        self.conv5 = nn.Conv2d(128, 64, kernel_size=3, padding=1)
        self.bn5 = nn.BatchNorm2d(128)

        self.conv6 = nn.Conv2d(96, 32, kernel_size=3, padding=1)
        self.bn6 = nn.BatchNorm2d(96)

        self.conv7 = nn.Conv2d(48, 16, kernel_size=3, padding=1)
        self.bn7 = nn.BatchNorm2d(48)

        self.conv_out = nn.Conv2d(16, channels[1], kernel_size=1)

        self.activation = nn.Sigmoid()

    def forward(self, x):
        # Encoder
        x1 = self.conv1(x)
        x1 = nn.ReLU()(self.bn1(x1))

        pool1 = self.pool(x1)
        x2 = self.conv2(pool1)
        x2 = nn.ReLU()(self.bn2(x2))

        pool2 = self.pool(x2)
        x3 = self.conv3(pool2)
        x3 = nn.ReLU()(self.bn3(x3))

        pool3 = self.pool(x3)
        x4 = self.conv4(pool3)
        x4 = nn.ReLU()(self.bn4(x4))

        pool_deep = self.pool(x4)
        xdeep = self.conv_deep(pool_deep)
        xdeep = nn.ReLU()(self.bn_deep(xdeep))

        # Decoder
        updeep = self.upsample(xdeep)
        mergedeep = torch.cat([x4, updeep], dim=1)

        xdeep2 = self.conv_deep2(mergedeep)
        xdeep2 = nn.ReLU()(self.bn_deep2(xdeep2))

        up5 = self.upsample(xdeep2)
        merge5 = torch.cat([x3, up5], dim=1)
        merge5 = self.bn5(merge5)

        x5 = self.conv5(merge5)
        x5 = nn.ReLU()(x5)

        up6 = self.upsample(x5)
        merge6 = torch.cat([x2, up6], dim=1)
        merge6 = self.bn6(merge6)

        x6 = self.conv6(merge6)
        x6 = nn.ReLU()(x6)

        up7 = self.upsample(x6)
        merge7 = torch.cat([x1, up7], dim=1)
        merge7 = self.bn7(merge7)

        x7 = self.conv7(merge7)
        output = self.conv_out(x7)

        return self.activation(output)