import logging
from functools import partial

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from models import register
from .mmseg.models.sam import ImageEncoderViT, TwoWayTransformer, PromptEncoder, TwoWayTransformerVisualSampler, VIT_MLAHead_h
# from .mmseg.models.sam import MaskDecoder
logger = logging.getLogger(__name__)
from .iou_loss import IOU
from typing import Any, Optional, Tuple


def init_weights(layer):
    if type(layer) == nn.Conv2d:
        nn.init.normal_(layer.weight, mean=0.0, std=0.02)
        nn.init.constant_(layer.bias, 0.0)
    elif type(layer) == nn.Linear:
        nn.init.normal_(layer.weight, mean=0.0, std=0.02)
        nn.init.constant_(layer.bias, 0.0)
    elif type(layer) == nn.BatchNorm2d:
        # print(layer)
        nn.init.normal_(layer.weight, mean=1.0, std=0.02)
        nn.init.constant_(layer.bias, 0.0)

class BBCEWithLogitLoss(nn.Module):
    '''
    Balanced BCEWithLogitLoss
    '''
    def __init__(self):
        super(BBCEWithLogitLoss, self).__init__()

    def forward(self, pred, gt):
        eps = 1e-10
        count_pos = torch.sum(gt) + eps
        count_neg = torch.sum(1. - gt)
        ratio = count_neg / count_pos
        w_neg = count_pos / (count_pos + count_neg)

        bce1 = nn.BCEWithLogitsLoss(pos_weight=ratio)
        loss = w_neg * bce1(pred, gt)

        return loss

def _iou_loss(pred, target):
    pred = torch.sigmoid(pred)
    inter = (pred * target).sum(dim=(2, 3))
    union = (pred + target).sum(dim=(2, 3)) - inter
    iou = 1 - (inter / union)

    return iou.mean()

class PositionEmbeddingRandom(nn.Module):
    """
    Positional encoding using random spatial frequencies.
    """

    def __init__(self, num_pos_feats: int = 64, scale: Optional[float] = None) -> None:
        super().__init__()
        if scale is None or scale <= 0.0:
            scale = 1.0
        self.register_buffer(
            "positional_encoding_gaussian_matrix",
            scale * torch.randn((2, num_pos_feats)),
        )

    def _pe_encoding(self, coords: torch.Tensor) -> torch.Tensor:
        """Positionally encode points that are normalized to [0,1]."""
        # assuming coords are in [0, 1]^2 square and have d_1 x ... x d_n x 2 shape
        coords = 2 * coords - 1
        coords = coords @ self.positional_encoding_gaussian_matrix
        coords = 2 * np.pi * coords
        # outputs d_1 x ... x d_n x C shape
        return torch.cat([torch.sin(coords), torch.cos(coords)], dim=-1)

    def forward(self, size: int) -> torch.Tensor:
        """Generate positional encoding for a grid of the specified size."""
        h, w = size, size
        device: Any = self.positional_encoding_gaussian_matrix.device
        grid = torch.ones((h, w), device=device, dtype=torch.float32)
        y_embed = grid.cumsum(dim=0) - 0.5
        x_embed = grid.cumsum(dim=1) - 0.5
        y_embed = y_embed / h
        x_embed = x_embed / w

        pe = self._pe_encoding(torch.stack([x_embed, y_embed], dim=-1))
        return pe.permute(2, 0, 1)  # C x H x W


@register('sam')
class SAM(nn.Module):
    def __init__(self, inp_size=None, encoder_mode=None, loss=None):
        super().__init__()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        #print('encoder_mode : ', encoder_mode)
        self.embed_dim = encoder_mode['embed_dim']
        self.image_encoder = ImageEncoderViT(
            img_size=inp_size,
            patch_size=encoder_mode['patch_size'],
            in_chans=3,
            embed_dim=encoder_mode['embed_dim'],
            depth=encoder_mode['depth'],
            num_heads=encoder_mode['num_heads'],
            mlp_ratio=encoder_mode['mlp_ratio'],
            out_chans=encoder_mode['out_chans'],
            qkv_bias=encoder_mode['qkv_bias'],
            norm_layer=partial(torch.nn.LayerNorm, eps=1e-6),
            act_layer=nn.GELU,
            use_rel_pos=encoder_mode['use_rel_pos'],
            rel_pos_zero_init=True,
            window_size=encoder_mode['window_size'],
            global_attn_indexes=encoder_mode['global_attn_indexes'],
        )
        self.prompt_embed_dim = encoder_mode['prompt_embed_dim']
        #self.batch_size = self.gt_mask.size()[0]
        # self.mask_decoder = MaskDecoder(
        #     num_multimask_outputs=3,
        #     transformer=TwoWayTransformer(
        #         depth=2,
        #         embedding_dim=self.prompt_embed_dim,
        #         mlp_dim=2048,
        #         num_heads=8,
        #     ),
        #     transformer_dim=self.prompt_embed_dim,
        #     iou_head_depth=3,
        #     iou_head_hidden_dim=256,
        # )

        # ------- modification start --------------
        self.prompt_encoder_list = []
        self.prompt_encoder = PromptEncoder(transformer=TwoWayTransformerVisualSampler(depth=2,
                                                                    embedding_dim=256,
                                                                    mlp_dim=2048,
                                                                    num_heads=8))
        for i in range(4):
            self.prompt_encoder.to(self.device)
            self.prompt_encoder_list.append(self.prompt_encoder)
        #parameter_list.extend([i for i in self.prompt_encoder.parameters() if i.requires_grad == True])
        
        #  Mask Decoder
        
        self.mask_decoder = VIT_MLAHead_h(img_size=64, num_classes=2)
        self.mask_decoder.to(self.device)
         
        # ------- modification done --------------

        # for k, p in self.image_encoder.named_parameters():
        #     if p.requires_grad:
        #         print(k)

        if 'evp' in encoder_mode['name']:
            for k, p in self.image_encoder.named_parameters():
                if "prompt" not in k and "mask_decoder" not in k and "prompt_encoder" not in k:
                    p.requires_grad = False

        # print("*" * 50)
        # for k, p in self.image_encoder.named_parameters():
        #     if p.requires_grad:
        #         print(k)


        self.loss_mode = loss
        if self.loss_mode == 'bce':
            self.criterionBCE = torch.nn.BCEWithLogitsLoss()

        elif self.loss_mode == 'bbce':
            self.criterionBCE = BBCEWithLogitLoss()

        elif self.loss_mode == 'iou':
            self.criterionBCE = torch.nn.BCEWithLogitsLoss()
            self.criterionIOU = IOU()

        self.pe_layer = PositionEmbeddingRandom(encoder_mode['prompt_embed_dim'] // 2)
        self.inp_size = inp_size
        self.image_embedding_size = inp_size // encoder_mode['patch_size']
        self.no_mask_embed = nn.Embedding(1, encoder_mode['prompt_embed_dim'])

    def set_input(self, input, gt_mask):
        self.input = input.to(self.device)
        self.gt_mask = gt_mask.to(self.device)

    def get_dense_pe(self) -> torch.Tensor:
        """
        Returns the positional encoding used to encode point prompts,
        applied to a dense set of points the shape of the image encoding.

        Returns:
          torch.Tensor: Positional encoding with shape
            1x(embed_dim)x(embedding_h)x(embedding_w)
        """
        return self.pe_layer(self.image_embedding_size).unsqueeze(0)


    def forward(self):
        bs = 1

        # print("img: ", self.input.size())   # [1, 3, 1024, 1024]
        # print('gts: ', self.gt_mask.size()) # [1, 1, 1024, 1024]
        # # Embed prompts
        sparse_embeddings = torch.empty((bs, 0, self.prompt_embed_dim), device=self.input.device)
        dense_embeddings = self.no_mask_embed.weight.reshape(1, -1, 1, 1).expand(
            bs, -1, self.image_embedding_size, self.image_embedding_size
        )

        # modification -------!!----------#

        self.features, feature_list = self.image_encoder(self.input)
        feature_list.append(self.features)
        feature_list = feature_list[::-1]

        # print('mask size', self.gt_mask.size())  # (B, 1, 1024, 1024)
        # print('mask: \n', self.gt_mask)
        # print(torch.where(self.gt_mask == 1))
        l = len(torch.where(self.gt_mask == 1)[2])
        batch_size = self.gt_mask.size()[0]
        points_torch = None
        if l > 0:
            sample = np.random.choice(np.arange(l), 20, replace=True)
            x = torch.where(self.gt_mask == 1)[2][sample].unsqueeze(1)  # (20, 1)
            y = torch.where(self.gt_mask == 1)[3][sample].unsqueeze(1)  # (20, 1)
            points = torch.cat([x, y], dim=1).unsqueeze(1).float() # (20, 1, 2)
            points_torch = points.to(self.device)
            points_torch = points_torch.transpose(0,1).repeat(batch_size, 1, 1)    # (b, 20, 2)

        
        new_feature = []
        for i, (feature, prompt_encoder) in enumerate(zip(feature_list, self.prompt_encoder_list)):
            if i == 3: # 第四层feature过prompt encoder
                new_feature.append(
                    prompt_encoder(feature, points_torch.clone())
                )
            else:
                new_feature.append(feature)
        img_resize = F.interpolate(self.input[:, 0].unsqueeze(1).to(self.device), scale_factor=self.features.shape[2]/self.input.shape[2],
                                           mode='bilinear')
        
        new_feature.append(img_resize)
        # for feat in new_feature:
        #     print("feat size: ", feat.size())
        #     [1, 256, 64, 64])
        #     [1, 256, 64, 64]
        #     [1, 256, 64, 64]
        #     [1, 256, 64, 64]
        #     [1, 1, 64, 64]
        low_res_masks = self.mask_decoder(new_feature, 1, 256//64)
        
        # -------modification done------------#

        # Predict masks
        # low_res_masks, iou_predictions = self.mask_decoder(
        #     image_embeddings=self.features,
        #     image_pe=self.get_dense_pe(),
        #     sparse_prompt_embeddings=sparse_embeddings,
        #     dense_prompt_embeddings=dense_embeddings,
        #     multimask_output=False,
        # )
        #print('los mask size: ', low_res_masks.size()) # [1, 1, 256, 256]
        # Upscale the masks to the original image resolution
        masks = self.postprocess_masks(low_res_masks, self.inp_size, self.inp_size)
        self.pred_mask = masks

    def infer(self, input):
        bs = 1

        # Embed prompts
        # sparse_embeddings = torch.empty((bs, 0, self.prompt_embed_dim), device=input.device)
        # dense_embeddings = self.no_mask_embed.weight.reshape(1, -1, 1, 1).expand(
        #     bs, -1, self.image_embedding_size, self.image_embedding_size
        # )

        # self.features = self.image_encoder(input)

        # # Predict masks
        # low_res_masks, iou_predictions = self.mask_decoder(
        #     image_embeddings=self.features,
        #     image_pe=self.get_dense_pe(),
        #     sparse_prompt_embeddings=sparse_embeddings,
        #     dense_prompt_embeddings=dense_embeddings,
        #     multimask_output=False,
        # )

        # # Upscale the masks to the original image resolution
        # masks = self.postprocess_masks(low_res_masks, self.inp_size, self.inp_size)

        ##  modification ----!!!!!!!! ###

        # image embeddings
        self.features, feature_list = self.image_encoder(self.input)
        feature_list.append(self.features)
        feature_list = feature_list[::-1]

        # embed prompts
        points_torch = None
        #prompt = F.interpolate(self.gt_mask[None, None, :, :], self.input.shape[2:], mode="nearest")[0]
        prompt = self.gt_mask
        #print("prompt ", prompt.size())
        l = len(torch.where(prompt == 1)[0])
        sample = np.random.choice(np.arange(l), 1, replace=True)
        x = torch.where(self.gt_mask == 1)[2][sample].unsqueeze(1)  # (1, 1)
        y = torch.where(self.gt_mask == 1)[3][sample].unsqueeze(1)  # (1, 1)
        points = torch.cat([x, y], dim=1).unsqueeze(1).float() # (1, 1, 2)
        points_torch = points.to(self.device)
        batch_size = self.gt_mask.size()[0]
        points_torch = points_torch.transpose(0,1).repeat(batch_size, 1, 1)
        
        #print("points ", points.size())
        

        new_feature = []
        for i, (feature, prompt_encoder) in enumerate(zip(feature_list, self.prompt_encoder_list)):
            if i == 3: # 第四层feature过prompt encoder
                new_feature.append(
                    prompt_encoder(feature, points_torch.clone())
                )
            else:
                new_feature.append(feature)
        img_resize = F.interpolate(self.input[:, 0].unsqueeze(1).to(self.device), scale_factor=self.features.shape[2]/self.input.shape[2],
                                           mode='bilinear')
        
        new_feature.append(img_resize)
        
        
        low_res_masks = self.mask_decoder(new_feature, 1, 256//64)
        masks = self.postprocess_masks(low_res_masks, self.inp_size, self.inp_size)
        #print("low res : ", low_res_masks.size())
        #print('masks ', masks.size())
        
        return masks

    def postprocess_masks(
        self,
        masks: torch.Tensor,
        input_size: Tuple[int, ...],
        original_size: Tuple[int, ...],
    ) -> torch.Tensor:
        """
        Remove padding and upscale masks to the original image size.

        Arguments:
          masks (torch.Tensor): Batched masks from the mask_decoder,
            in BxCxHxW format.
          input_size (tuple(int, int)): The size of the image input to the
            model, in (H, W) format. Used to remove padding.
          original_size (tuple(int, int)): The original size of the image
            before resizing for input to the model, in (H, W) format.

        Returns:
          (torch.Tensor): Batched masks in BxCxHxW format, where (H, W)
            is given by original_size.
        """
        masks = F.interpolate(
            masks,
            (self.image_encoder.img_size, self.image_encoder.img_size),
            mode="bilinear",
            align_corners=False,
        )
        masks = masks[..., : input_size, : input_size]
        masks = F.interpolate(masks, original_size, mode="bilinear", align_corners=False)
        return masks

    def backward_G(self):
        """Calculate GAN and L1 loss for the generator"""
        self.loss_G = self.criterionBCE(self.pred_mask, self.gt_mask)
        if self.loss_mode == 'iou':
            self.loss_G += _iou_loss(self.pred_mask, self.gt_mask)

        self.loss_G.backward()

    def optimize_parameters(self):
        self.forward()
        self.optimizer.zero_grad()  # set G's gradients to zero
        self.backward_G()  # calculate graidents for G
        self.optimizer.step()  # udpate G's weights

    def set_requires_grad(self, nets, requires_grad=False):
        """Set requies_grad=Fasle for all the networks to avoid unnecessary computations
        Parameters:
            nets (network list)   -- a list of networks
            requires_grad (bool)  -- whether the networks require gradients or not
        """
        if not isinstance(nets, list):
            nets = [nets]
        for net in nets:
            if net is not None:
                for param in net.parameters():
                    param.requires_grad = requires_grad
