import math

import torch
from comfy.utils import common_upscale

try:
    from nodes import MAX_RESOLUTION
except ImportError:
    MAX_RESOLUTION = 16384

# Matches KJNodes' BBOX type string so this node's bbox output plugs directly
# into KJNodes' "Image Uncrop By Mask".
BBOX_TYPES = "BBOX,BOUNDING_BOX"

ASPECT_RATIOS = {
    "16:9": 16 / 9,
    "4:3": 4 / 3,
    "1:1": 1 / 1,
    "3:4": 3 / 4,
    "9:16": 9 / 16,
    "21:9": 21 / 9,
}
RESOLVED_ASPECT_RATIO_NAMES = list(ASPECT_RATIOS.keys())
ASPECT_RATIO_CHOICES = RESOLVED_ASPECT_RATIO_NAMES + ["adaptive"]


def _round_to_multiple(value, multiple=16):
    return max(multiple, int(round(value / multiple)) * multiple)


def _mask_center_and_padded_size(mask_frame, padding):
    """Bounding box of a single mask frame, expanded by `padding` and clamped
    to the frame's own bounds. Returns (x_center, y_center, width, height)."""
    h0, w0 = mask_frame.shape
    iy, ix = (mask_frame == 1).nonzero(as_tuple=True)

    if iy.numel() == 0:
        x_c, y_c = w0 / 2.0, h0 / 2.0
        width = height = 0
    else:
        x_min, x_max = ix.min().item(), ix.max().item()
        y_min, y_max = iy.min().item(), iy.max().item()
        width = x_max - x_min + 1
        height = y_max - y_min + 1
        x_c = (x_min + x_max) / 2.0
        y_c = (y_min + y_max) / 2.0

    pad_x = min((w0 - width) // 2, padding)
    pad_y = min((h0 - height) // 2, padding)

    final_width = min(width + 2 * pad_x, w0)
    final_height = min(height + 2 * pad_y, h0)

    # Guard against a fully empty mask producing a zero-area crop.
    final_width = max(final_width, 1)
    final_height = max(final_height, 1)

    return x_c, y_c, final_width, final_height


def _closest_aspect_ratio_name(ratio):
    return min(
        RESOLVED_ASPECT_RATIO_NAMES,
        key=lambda name: abs(math.log(ratio) - math.log(ASPECT_RATIOS[name])),
    )


def _fit_to_ratio(w, h, target_ratio, max_w, max_h):
    """Smallest box with target_ratio that fully contains a w x h box,
    clamped to (max_w, max_h) without breaking the ratio."""
    if w / h > target_ratio:
        crop_w, crop_h = w, w / target_ratio
    else:
        crop_h, crop_w = h, h * target_ratio

    if crop_w > max_w or crop_h > max_h:
        scale = min(max_w / crop_w, max_h / crop_h)
        crop_w *= scale
        crop_h *= scale

    return max(1, min(int(round(crop_w)), max_w)), max(1, min(int(round(crop_h)), max_h))


class ImageCropByMaskToAspect:
    """
    Batch-aware version of KJNodes' "Image Crop By Mask And Resize" that fits
    the crop to one of a fixed set of aspect ratios instead of an arbitrary
    one derived from the mask. The whole batch is analysed together (the mask
    can move/change per frame, e.g. in a video), and the crop window is built
    to already match the target aspect ratio before the final resize, so the
    resize step is a pure uniform scale and never stretches the image.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "mask": ("MASK",),
                "aspect_ratio": (ASPECT_RATIO_CHOICES, {"default": "adaptive"}),
                "crop_mode": (["tight", "uniform"], {"default": "tight"}),
                "base_resolution": ("INT", {"default": 512, "min": 64, "max": MAX_RESOLUTION, "step": 8}),
                "padding": ("INT", {"default": 0, "min": 0, "max": MAX_RESOLUTION, "step": 1}),
            },
        }

    RETURN_TYPES = ("IMAGE", "MASK", BBOX_TYPES, RESOLVED_ASPECT_RATIO_NAMES)
    RETURN_NAMES = ("images", "masks", "bbox", "aspect_ratio")
    FUNCTION = "crop"
    CATEGORY = "image/crop"
    DESCRIPTION = (
        "Crops a batch of images by their masks (+ padding), fitting the crop to the "
        "closest of a fixed set of aspect ratios (or a chosen one), analysing the "
        "whole batch so a moving mask still gets one consistent aspect ratio. "
        "crop_mode='tight' sizes each frame's crop window to its own mask (minimal "
        "margin, zoom level can vary frame to frame); 'uniform' uses one crop "
        "window size for the whole batch (stable zoom, but looser on frames whose "
        "mask is smaller than the batch's largest)."
    )

    def crop(self, image, mask, aspect_ratio, crop_mode, base_resolution, padding=0):
        mask = mask.round()
        B, H, W, _ = image.shape

        # Step 1: per-frame mask bbox center + padded size.
        centers = []
        for i in range(B):
            centers.append(_mask_center_and_padded_size(mask[i], padding))

        max_w = max(c[2] for c in centers)
        max_h = max(c[3] for c in centers)
        envelope_ratio = max_w / max_h

        # Step 2: resolve the target aspect ratio for the whole batch.
        if aspect_ratio == "adaptive":
            target_name = _closest_aspect_ratio_name(envelope_ratio)
        else:
            target_name = aspect_ratio
        target_ratio = ASPECT_RATIOS[target_name]

        # Step 3: crop window size(s) that already match target_ratio.
        # uniform: one window, sized to contain the batch's largest mask, shared
        # by every frame (stable zoom level across the clip).
        # tight: each frame gets its own window, sized to its own mask only
        # (minimal margin, but the effective zoom can vary frame to frame).
        if crop_mode == "uniform":
            uniform_crop_w, uniform_crop_h = _fit_to_ratio(max_w, max_h, target_ratio, W, H)

        # Step 4: final output size from base_resolution (shorter side),
        # snapped to a multiple of 16.
        if target_ratio >= 1:
            target_height = base_resolution
            target_width = base_resolution * target_ratio
        else:
            target_width = base_resolution
            target_height = base_resolution / target_ratio
        target_width = _round_to_multiple(target_width)
        target_height = _round_to_multiple(target_height)

        # Step 5: crop + resize each frame, centered on that frame's own mask.
        image_list, mask_list, bbox_list = [], [], []
        for i in range(B):
            x_c, y_c, w_i, h_i = centers[i]

            if crop_mode == "uniform":
                crop_w, crop_h = uniform_crop_w, uniform_crop_h
            else:
                crop_w, crop_h = _fit_to_ratio(w_i, h_i, target_ratio, W, H)

            x0 = max(0, min(int(round(x_c - crop_w / 2)), W - crop_w))
            y0 = max(0, min(int(round(y_c - crop_h / 2)), H - crop_h))
            x1, y1 = x0 + crop_w, y0 + crop_h

            cropped_image = image[i][y0:y1, x0:x1, :]
            cropped_mask = mask[i][y0:y1, x0:x1]

            cropped_image = cropped_image.unsqueeze(0).movedim(-1, 1)
            cropped_image = common_upscale(cropped_image, target_width, target_height, "lanczos", "disabled")
            cropped_image = cropped_image.movedim(1, -1).squeeze(0)

            cropped_mask = cropped_mask.unsqueeze(0).unsqueeze(0)
            cropped_mask = common_upscale(cropped_mask, target_width, target_height, "bilinear", "disabled")
            cropped_mask = cropped_mask.squeeze(0).squeeze(0)

            image_list.append(cropped_image)
            mask_list.append(cropped_mask)
            bbox_list.append((x0, y0, x1, y1))

        return (torch.stack(image_list), torch.stack(mask_list), bbox_list, target_name)


NODE_CLASS_MAPPINGS = {
    "ImageCropByMaskToAspect": ImageCropByMaskToAspect,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ImageCropByMaskToAspect": "Image Crop By Mask To Aspect Ratio",
}
