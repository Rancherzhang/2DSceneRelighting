# -*- coding: utf-8 -*-

import torch
import torch.nn as nn
from abc import ABC
from numpy import array, abs


EMPTY_TENSOR = torch.Tensor()


# =======
# Encoder
# =======

class SkipConnectionProvider(nn.Module, ABC):
    def __init__(self):
        super(SkipConnectionProvider, self).__init__()
        self.skip_connections = []

    def forward(self, x):
        return NotImplemented

    def _reset_skip_connections(self):
        self.skip_connections = []

    def get_skip_connections(self):
        # should not require tensor copying (based on https://github.com/milesial/Pytorch-UNet)
        return self.skip_connections
    
    
class ImageEncoding(SkipConnectionProvider):
    def __init__(self, in_channels=3, enc_channels=32, out_channels=64):
        super(ImageEncoding, self).__init__()
        self.conv1 = nn.Sequential(
            nn.Conv2d(in_channels, enc_channels - in_channels, 7, padding=3),
            nn.BatchNorm2d(enc_channels - in_channels),
            nn.PReLU()
         )
        self.conv2 = nn.Sequential(
            nn.Conv2d(enc_channels, out_channels, 3, stride=2, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.PReLU()
         )
    
    def forward(self, image):
        x = self.conv1(image)
        encoded_image = torch.cat((x, image), dim=1)  # append image channels to the convolution result
        self.skip_connections = [encoded_image]
        return self.conv2(encoded_image)


class EncDoubleConv(SkipConnectionProvider):
    def __init__(self, in_channels, out_channels):
        super(EncDoubleConv, self).__init__()
        self.conv1 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, 3, padding=1),
            nn.BatchNorm2d(in_channels),
            nn.PReLU()
        )
        self.conv2 = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, stride=2, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.PReLU()
        )

    def forward(self, x):
        self._reset_skip_connections()

        self.skip_connections.append(x)
        x = self.conv1(x)
        self.skip_connections.append(x)
        return self.conv2(x)


class EncBottleneckConv(SkipConnectionProvider):
    def __init__(self, in_channels, depth=4):
        super(EncBottleneckConv, self).__init__()
        self.convolutions = self._build_bottleneck_convolutions(in_channels, depth)

    @staticmethod
    def _build_bottleneck_convolutions(in_channels, depth):
        single_conv = [nn.Sequential(
            nn.Conv2d(in_channels, in_channels, 3, padding=1),
            nn.BatchNorm2d(in_channels),
            nn.PReLU()
        )]
        convolutions = single_conv * (depth - 1)

        return nn.ModuleList(convolutions)

    def forward(self, x):
        self._reset_skip_connections()

        for module in self.convolutions:
            self.skip_connections.append(x)
            x = module(x)

        return x


class Encoder(SkipConnectionProvider):
    def __init__(self, n_double_conv=3, bottleneck_depth=4, disabled_skip_connections=None):
        super(Encoder, self).__init__()
        self.disabled_skip_connections = disabled_skip_connections if disabled_skip_connections is not None else []
        self.image_encoder = ImageEncoding()
        self.double_convolutions = self._build_encoder_convolutions_block(n_double_conv)
        self.bottleneck = EncBottleneckConv(512, bottleneck_depth)
        self.n_skip_connections = 1 + n_double_conv * 2 + (bottleneck_depth - 1)

    @staticmethod
    def _build_encoder_convolutions_block(n):
        return nn.ModuleList([EncDoubleConv(64 * (2 ** i), 64 * (2 ** (i + 1))) for i in range(n)])

    def forward(self, image):
        self._reset_skip_connections()

        # initial image encoding
        x = self.image_encoder(image)
        self.skip_connections += self.image_encoder.get_skip_connections()

        # double convolution layers
        for double_conv in self.double_convolutions:
            x = double_conv(x)
            self.skip_connections += double_conv.get_skip_connections()

        # pre-bottleneck layer
        x = self.bottleneck(x)
        self.skip_connections += self.bottleneck.get_skip_connections()

        return x

    def get_skip_connections(self):
        for i in self.disabled_skip_connections:
            self.skip_connections[i] = EMPTY_TENSOR
        return self.skip_connections

    def get_number_of_possible_skip_connections(self):
        return self.n_skip_connections


# ===========================
# IlluminationSwapNetSplitter
# ===========================

class WeightedPooling(nn.Module):
    def __init__(self, in_channels=512, envmap_h=16, envmap_w=32):
        expected_channels = envmap_h * envmap_w
        assert in_channels == expected_channels, \
            f'WeightedPooling input has {in_channels} channels, expected {expected_channels}'
        
        super(WeightedPooling, self).__init__()
        
        # final conv before weighted average
        out_channels = 4 * in_channels
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.Softplus()
        )

    def forward(self, x):
        x = self.conv(x)
        
        # split x into environment map predictions and confidence
        channels = x.size()[1]
        split_point = 3 * (channels // 4)
        envmap_predictions, confidence = x[:, :split_point], x[:, split_point:]
        
        # TODO: multiplication with sum can probably be implemented as convolution with groups (according to some posts)
        return (envmap_predictions * confidence.repeat((1, 3, 1, 1))).sum(dim=(2, 3), keepdim=True)


class Tiling(nn.Module):
    def __init__(self, size=16, in_channels=1536, out_channels=512):
        super(Tiling, self).__init__()
        self.size = size
        self.encode = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.PReLU()
        )

    def forward(self, x):
        tiled = x.repeat((1, 1, self.size, self.size))
        return self.encode(tiled)


class IlluminationSwapNetSplitter(nn.Module):
    def __init__(self):
        super(IlluminationSwapNetSplitter, self).__init__()        
        self.weighted_pool = WeightedPooling()

    def forward(self, latent):        
        pred_env_map = self.weighted_pool(latent)
        scene_latent = None
        light_latent = pred_env_map
        return scene_latent, light_latent


class IlluminationSwapNetAssembler(nn.Module):
    def __init__(self):
        super(IlluminationSwapNetAssembler, self).__init__()
        self.tile = Tiling()

    def forward(self, scene_latent, light_latent):        
        latent = self.tile(light_latent)
        return latent


# ======================
# AnOtherSwapNetSplitter
# ======================

def size_splits(tensor, split_sizes, dim=0):
    # https://github.com/pytorch/pytorch/issues/3223
    """Splits the tensor according to chunks of split_sizes.
    
    Arguments:
        tensor (Tensor): tensor to split.
        split_sizes (list(int)): sizes of chunks
        dim (int): dimension along which to split the tensor.
    """
    if dim < 0:
        dim += tensor.dim()
    
    dim_size = tensor.size(dim)
    if dim_size != torch.sum(torch.Tensor(split_sizes)):
        raise KeyError("Sum of split sizes exceeds tensor dim")
    
    splits = torch.cumsum(torch.Tensor([0] + split_sizes), dim=0)[:-1]

    return tuple(tensor.narrow(int(dim), int(start), int(length)) for start, length in zip(splits, split_sizes))


class AnOtherSwapNetSplitter(nn.Module):
    def __init__(self):
        super(AnOtherSwapNetSplitter, self).__init__()

    def forward(self, latent):
        light_latent, scene_latent = size_splits(latent, [3, 509], dim=1)
        return scene_latent, light_latent


class AnOtherSwapNetAssembler(nn.Module):
    def __init__(self):
        super(AnOtherSwapNetAssembler, self).__init__()

    def forward(self, scene_latent, light_latent):    
        latent = torch.cat((light_latent, scene_latent), dim=1)
        return latent

# ======================
# Splitter512x1x1
# ======================


class Splitter512x1x1(nn.Module):
    def __init__(self):
        super(Splitter512x1x1, self).__init__() 
        self.to1 = nn.Sequential(
            nn.MaxPool2d(kernel_size=16),
            nn.BatchNorm2d(512),
            nn.PReLU()
        )

    def forward(self, latent):      
        latent = self.to1(latent) 
        light_latent, scene_latent = size_splits(latent, [16, 512-16], dim=1)
        return scene_latent, light_latent


class Assembler512x1x1(nn.Module):
    def __init__(self):
        super(Assembler512x1x1, self).__init__()
        self.to16 = nn.Sequential(
            nn.Upsample(size=(16, 16)),
            nn.BatchNorm2d(512),
            nn.PReLU()
        )

    def forward(self, scene_latent, light_latent):    
        latent = torch.cat((light_latent, scene_latent), dim=1)
        latent = self.to16(latent) 
        return latent


# =======
# Decoder
# =======

class SkipConnectionReceiver(nn.Module, ABC):
    def __init__(self, disabled_skip_connections, start=0):
        super(SkipConnectionReceiver, self).__init__()
        self.disabled_skip_connections = disabled_skip_connections - start \
            if disabled_skip_connections is not None else array([])

    def channels_in_layer(self, layer_number, layer_channels, skip_connection_channels):
        if layer_number in self.disabled_skip_connections:
            return layer_channels
        return layer_channels + skip_connection_channels

    @staticmethod
    def channel_concat(x, y):
        return torch.cat((x, y), dim=1)

    def forward(self, x, skip_connections):
        return NotImplemented
    

class DecBottleneckConv(SkipConnectionReceiver):
    def __init__(self, in_channels, envmap_h=16, envmap_w=32, depth=4, disabled_skip_connections=None):
        expected_channels = envmap_h * envmap_w
        assert depth >= 2, f'Depth should be not smaller than 3'
        assert in_channels == expected_channels, \
            f'UpBottleneck input has {in_channels} channels, expected {expected_channels}'
        super(DecBottleneckConv, self).__init__(disabled_skip_connections, 0)
        self.depth = depth

        half_in_channels = in_channels // 2
        self.encode = nn.Sequential(
            nn.Conv2d(in_channels, half_in_channels, 3, padding=1),
            nn.BatchNorm2d(half_in_channels),
            nn.PReLU()
        )
        initial_in_channels = self.channels_in_layer(0, half_in_channels, in_channels)
        self.initial_conv = nn.Sequential(
            nn.Conv2d(initial_in_channels, in_channels, 3, padding=1),
            nn.BatchNorm2d(in_channels),
            nn.PReLU()
        )
        self.convs = nn.ModuleList()
        for i in range(depth - 3):
            conv_in_channels = self.channels_in_layer(i + 1, in_channels, in_channels)
            self.convs.append(nn.Sequential(
                nn.Conv2d(conv_in_channels, in_channels, 3, padding=1),
                nn.BatchNorm2d(in_channels),
                nn.PReLU()
            ))
        out_conv_in_channels = self.channels_in_layer(depth - 2, in_channels, in_channels)
        self.out_conv = nn.Sequential(
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(out_conv_in_channels, half_in_channels, 3, padding=1),
            nn.BatchNorm2d(half_in_channels),
            nn.PReLU()
        )

    def forward(self, x, skip_connections):
        # encoding convolution
        x = self.encode(x)

        # transposed convolutions with skip connections
        x = self.initial_conv(self.channel_concat(x, skip_connections.pop()))
        for conv in self.convs:
            x = conv(self.channel_concat(x, skip_connections.pop()))
        return self.out_conv(self.channel_concat(x, skip_connections.pop()))


class DecDoubleConv(SkipConnectionReceiver):
    def __init__(self, in_channels, out_channels, disabled_skip_connections=None, start=0):
        super(DecDoubleConv, self).__init__(disabled_skip_connections, start)
        conv1_in_channels = self.channels_in_layer(0, in_channels, in_channels)
        self.conv1 = nn.Sequential(
            nn.Conv2d(conv1_in_channels, in_channels, 3, padding=1),
            nn.BatchNorm2d(in_channels),
            nn.PReLU()
        )
        conv2_in_channels = self.channels_in_layer(1, in_channels, in_channels)
        self.conv2 = nn.Sequential(
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(conv2_in_channels, out_channels, 3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.PReLU()
        )

    def forward(self, x, skip_connections):
        x = self.conv1(self.channel_concat(x, skip_connections.pop()))
        return self.conv2(self.channel_concat(x, skip_connections.pop()))


class Output(SkipConnectionReceiver):
    def __init__(self, in_channels=32, out_channels=3, kernel_size=3, disabled_skip_connections=None, start=0):
        super(Output, self).__init__(disabled_skip_connections, start)
        output_in_channels = self.channels_in_layer(0, in_channels, in_channels)
        self.block = nn.Sequential(
            # should it be conv or transposed conv?
            nn.Conv2d(output_in_channels, out_channels, kernel_size, padding=kernel_size//2),
            nn.Sigmoid()
        )

    def forward(self, x, encoded_img):
        return self.block(self.channel_concat(x, encoded_img))


class Decoder(nn.Module):
    def __init__(self, n_double_conv=3, bottleneck_depth=4, last_kernel_size=3, disabled_skip_connections=None):
        super(Decoder, self).__init__()
        self.bottleneck = DecBottleneckConv(512, depth=bottleneck_depth,
                                            disabled_skip_connections=disabled_skip_connections)
        double_convs_start = bottleneck_depth - 1
        self.double_convolutions = self._build_decoder_convolutions_block(n_double_conv,
                                                                          disabled_skip_connections,
                                                                          double_convs_start)

        output_start = double_convs_start + n_double_conv * 2
        self.output = Output(kernel_size=last_kernel_size,
                             disabled_skip_connections=disabled_skip_connections,
                             start=output_start)

    @staticmethod
    def _build_decoder_convolutions_block(n, disabled_skip_connections, start):
        return nn.ModuleList([DecDoubleConv(256 // (2 ** i), 256 // (2 ** (i + 1)),
                                            disabled_skip_connections=disabled_skip_connections,
                                            start=(start + 2*i)) for i in range(n)])

    def forward(self, latent, skip_connections):

        # post-bottleneck layer
        x = self.bottleneck(latent, skip_connections)

        # double convolution layers
        for double_conv in self.double_convolutions:
            x = double_conv(x, skip_connections)

        # final layer constructing image
        relit = self.output(x, skip_connections.pop())

        return relit


# =======
# SwapNet
# =======
        
class SwapNet(nn.Module):
    def __init__(self, splitter, assembler,
                 n_double_conv=3, bottleneck_depth=4, last_kernel_size=3,
                 disabled_skip_connections=None):
        super(SwapNet, self).__init__()
        disabled_skip_connections = array([]) if disabled_skip_connections is None else array(disabled_skip_connections)

        self.encode = Encoder(n_double_conv, bottleneck_depth, disabled_skip_connections)
        self.split = splitter
        self.assemble = assembler

        # "Reflect" skip connection numbers as they are counted in reversed order wrt encoder
        n = self.encode.get_number_of_possible_skip_connections()
        decoder_disabled_skip_connections = (n - 1) - disabled_skip_connections
        self.decode = Decoder(n_double_conv, bottleneck_depth, last_kernel_size,
                              decoder_disabled_skip_connections)

    def forward(self, image, target, groundtruth):
        # pass image through encoder
        image_latent = self.encode(image)
        image_skip_connections = self.encode.get_skip_connections()
        # pass target through encoder
        target_latent = self.encode(target)
        # pass ground-truth through encoder
        groundtruth_latent = self.encode(groundtruth)
        
        # pass image_latent through splitter
        image_scene_latent, image_light_latent = self.split(image_latent)
        # pass target_latent through splitter
        target_scene_latent, target_light_latent = self.split(target_latent)      
        # pass groundtruth_latent through splitter
        groundtruth_scene_latent, groundtruth_light_latent = self.split(groundtruth_latent)
        
        swapped_latent = self.assemble(image_scene_latent, target_light_latent)

        # decode image with target env map
        relit_image = self.decode(swapped_latent, image_skip_connections)
        
        # decode image with its env map
        # reconstructed_image = self.decode(image_env_map, image_skip_connections)

        # pass relighted image second time through the network to get its env map
        # relighted_env_map = self.encode(relighted_image)

        return relit_image, \
            image_light_latent, target_light_latent, groundtruth_light_latent, \
            image_scene_latent, target_scene_latent, groundtruth_scene_latent


class IlluminationSwapNet(SwapNet):
    def __init__(self, last_kernel_size=3, disabled_skip_connections=None):
        """
        Illumination swap network model based on "Single Image Portrait Relighting" (Sun et al., 2019).
        Autoencoder accepts two images as inputs - one to be relit and one representing target lighting conditions.
        It learns to encode their environment maps in the latent representation. In the bottleneck the latent
        representations are swapped so that the decoder, using U-Net-like skip connections from the encoder,
        generates image with the original content but under the lighting conditions of the second input.
        """
        super(IlluminationSwapNet, self).__init__(splitter=IlluminationSwapNetSplitter(),
                                                  assembler=IlluminationSwapNetAssembler(),
                                                  last_kernel_size=last_kernel_size,
                                                  disabled_skip_connections=disabled_skip_connections)

    def forward(self, image, target, ground_truth):
        relit_image, \
            image_light_latent, target_light_latent, groundtruth_light_latent, \
            image_scene_latent, target_scene_latent, groundtruth_scene_latent = \
            super(IlluminationSwapNet, self).forward(image, target, ground_truth)
        return relit_image,\
            image_light_latent.view(-1, 3, 16, 32), \
            target_light_latent.view(-1, 3, 16, 32), groundtruth_light_latent.view(-1, 3, 16, 32), \
            image_scene_latent, target_scene_latent, groundtruth_scene_latent  # those are None


class AnOtherSwapNet(SwapNet):
    def __init__(self, last_kernel_size=3, disabled_skip_connections=None):
        super(AnOtherSwapNet, self).__init__(splitter=AnOtherSwapNetSplitter(),
                                             assembler=AnOtherSwapNetAssembler(),
                                             last_kernel_size=last_kernel_size,
                                             disabled_skip_connections=disabled_skip_connections)

    def forward(self, image, target, ground_truth):
        return super(AnOtherSwapNet, self).forward(image, target, ground_truth)


class SwapNet512x1x1(SwapNet):
    def __init__(self, last_kernel_size=3, disabled_skip_connections=None):
        super(SwapNet512x1x1, self).__init__(splitter=Splitter512x1x1(),
                                             assembler=Assembler512x1x1(),
                                             last_kernel_size=last_kernel_size,
                                             disabled_skip_connections=disabled_skip_connections)

    def forward(self, image, target, ground_truth):
        relit_image, \
            image_light_latent, target_light_latent, groundtruth_light_latent, \
            image_scene_latent, target_scene_latent, groundtruth_scene_latent = \
            super(SwapNet512x1x1, self).forward(image, target, ground_truth)
        return relit_image,\
            image_light_latent.view(-1, 1, 4, 4), \
            target_light_latent.view(-1, 1, 4, 4), groundtruth_light_latent.view(-1, 1, 4, 4),\
            image_scene_latent, target_scene_latent, groundtruth_scene_latent


# =====================
# IlluminationPredictor
# =====================

class IlluminationPredicter(nn.Module):
    def __init__(self, in_size=64*16*16, out_reals=2):
        super(IlluminationPredicter, self).__init__()
        self.in_size = in_size
        self.fc = nn.Linear(in_size, out_reals)

    def forward(self, x):
        x = x.view(-1, self.in_size)
        return self.fc(x)
        

class GroundtruthEnvmapSwapNet(SwapNet):
    """
    Illumination swap network model based on "Single Image Portrait Relighting" (Sun et al., 2019) using additional
    generated ground-truth environment maps.
    Autoencoder accepts two images as inputs - one to be relit and one representing target lighting conditions.
    It learns to encode their environment maps in the latent representation. In the bottleneck the latent
    representations are swapped so that the decoder, using U-Net-like skip connections from the encoder,
    generates image with the original content but under the lighting conditions of the second input.
    """
    def __init__(self, disabled_skip_connections=None):
        super(GroundtruthEnvmapSwapNet, self).__init__(splitter=IlluminationSwapNetSplitter(),
                                                       assembler=IlluminationSwapNetAssembler(),
                                                       disabled_skip_connections=disabled_skip_connections)

    def forward(self, image, target, groundtruth):
        # pass image & target through encoder
        image_latent = self.encode(image)
        image_skip_connections = self.encode.get_skip_connections()
        _, predicted_image_envmap = self.split(image_latent)
        target_latent = self.encode(target)
        _, predicted_target_envmap = self.split(target_latent)

        # decode from target envmap ground-truth using image skip connections
        swapped_latent = self.assemble(None, predicted_target_envmap)
        relit_image = self.decode(swapped_latent, image_skip_connections)

        return relit_image, predicted_image_envmap, predicted_target_envmap
