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


def _round_to_multiple(value, multiple):
    return max(multiple, int(round(value / multiple)) * multiple)


def _mask_center_and_padded_size(mask_frame, padding):
    """Bounding box of a single mask frame, expanded by `padding` and clamped
    to the frame's own bounds. Returns (x_center, y_center, width, height),
    or None when the mask is empty."""
    h0, w0 = mask_frame.shape
    iy, ix = (mask_frame == 1).nonzero(as_tuple=True)

    if iy.numel() == 0:
        return None

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


def _smooth(values, radius, hold_peaks=False, keep_trend=False):
    """Zero-lag Gaussian smoothing over time (one value per frame).

    Past the ends, the first/last frame is repeated, or with keep_trend the
    motion is mirrored around it, so steady movement doesn't slow down at the
    start/end of the clip. With hold_peaks, a sliding max over the same radius
    runs first, so the result never dips below any input value: a crop window
    that is about to grow starts growing early instead of cutting the mask off."""
    n = len(values)
    offsets = range(-radius, radius + 1)

    def at(seq, i):
        if 0 <= i < n:
            return seq[i]
        edge = 0 if i < 0 else n - 1
        if not keep_trend:
            return seq[edge]
        mirrored = min(max(2 * edge - i, 0), n - 1)
        return 2 * seq[edge] - seq[mirrored]

    if hold_peaks:
        values = [max(at(values, i + k) for k in offsets) for i in range(n)]
    sigma = radius / 2.0
    weights = [math.exp(-0.5 * (k / sigma) ** 2) for k in offsets]
    total = sum(weights)
    return [sum(w * at(values, i + k) for w, k in zip(weights, offsets)) / total for i in range(n)]


def _smooth_in_corridor(values, lows, highs, radius, passes=8):
    """Smooth a per-frame position while keeping each frame inside its own
    [low, high] range: smooth, clamp, and repeat, so the clamps don't leave
    kinks and the path settles into a smooth line through the corridor."""
    path = values
    for _ in range(passes):
        path = _smooth(path, radius, keep_trend=True)
        path = [min(max(v, lo), hi) for v, lo, hi in zip(path, lows, highs)]
    return path


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
                "divisible_by": ("INT", {"default": 1, "min": 1, "max": 256, "step": 1,
                                         "tooltip": "Snap both output sides to a multiple of this (1 = exact base_resolution; 16 for most video models)."}),
                "empty_mask": (["hold_last", "full_image"], {"default": "hold_last",
                                         "tooltip": "Frames with an empty mask: hold_last reuses the crop of the last frame that had a mask; full_image uses the whole image. A batch with no mask at all always uses the whole image."}),
                "smoothing": ("INT", {"default": 0, "min": 0, "max": 1000, "step": 1,
                                      "tooltip": "Smooths the crop's movement and zoom over time, in frames (0 = off). The mask always stays inside the crop."}),
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
        "mask is smaller than the batch's largest). empty_mask decides what frames "
        "without a mask show; smoothing evens out jumps in the crop over time."
    )

    def crop(self, image, mask, aspect_ratio, crop_mode, base_resolution, padding=0, divisible_by=1,
             empty_mask="hold_last", smoothing=0):
        mask = mask.round()
        B, H, W, _ = image.shape

        # Step 1: per-frame mask bbox center + padded size (None = empty mask).
        boxes = [_mask_center_and_padded_size(mask[i], padding) for i in range(B)]
        found = [b for b in boxes if b is not None]

        # Step 2: resolve the target aspect ratio for the whole batch, from the
        # frames that have a mask. With no mask anywhere, adaptive picks the
        # ratio closest to the image itself (so the output is the full frame).
        if found:
            max_w = max(b[2] for b in found)
            max_h = max(b[3] for b in found)
            envelope_ratio = max_w / max_h
        else:
            envelope_ratio = W / H
        if aspect_ratio == "adaptive":
            target_name = _closest_aspect_ratio_name(envelope_ratio)
        else:
            target_name = aspect_ratio
        target_ratio = ASPECT_RATIOS[target_name]

        # Step 3: empty frames. hold_last reuses the last frame that had a mask
        # (frames before the first mask take the first one); everything still
        # empty gets the largest target-ratio window centered in the image,
        # which is exactly the full frame when the ratios match.
        if empty_mask == "hold_last" and found:
            last = found[0]
            for i in range(B):
                if boxes[i] is None:
                    boxes[i] = last
                else:
                    last = boxes[i]
        full_w, full_h = _fit_to_ratio(W, H, target_ratio, W, H)

        # Step 4: crop window per frame as (x_center, y_center, width, height),
        # already matching target_ratio.
        # uniform: one window size, sized to contain the batch's largest mask,
        # shared by every frame (stable zoom level across the clip).
        # tight: each frame gets its own window, sized to its own mask only
        # (minimal margin, but the effective zoom can vary frame to frame).
        if crop_mode == "uniform" and found:
            uniform_crop_w, uniform_crop_h = _fit_to_ratio(max_w, max_h, target_ratio, W, H)
        windows = []
        for b in boxes:
            if b is None:
                windows.append((W / 2.0, H / 2.0, full_w, full_h))
            elif crop_mode == "uniform":
                windows.append((b[0], b[1], uniform_crop_w, uniform_crop_h))
            else:
                windows.append((b[0], b[1]) + _fit_to_ratio(b[2], b[3], target_ratio, W, H))

        # Step 4b: smoothing. Size and center are smoothed over time with no
        # lag. The size never drops below a frame's own window; the center is
        # smoothed inside the range that keeps that frame's padded mask (and
        # the image edges) within the window.
        if smoothing > 0 and B > 1:
            smooth_w = _smooth([w[2] for w in windows], smoothing, hold_peaks=True)
            sizes = []
            for i in range(B):
                crop_w = min(max(smooth_w[i], windows[i][2]), full_w)
                crop_h = max(crop_w / target_ratio, windows[i][3])
                sizes.append((crop_w, crop_h))

            def corridor(axis, limit):
                lows, highs = [], []
                for b, size in zip(boxes, sizes):
                    half = size[axis] / 2
                    lo, hi = half, limit - half
                    if b is not None:
                        center, extent = b[axis], b[axis + 2]
                        lo = max(lo, center + extent / 2 - half)
                        hi = min(hi, center - extent / 2 + half)
                    if lo > hi:
                        lo = hi = (lo + hi) / 2
                    lows.append(lo)
                    highs.append(hi)
                return lows, highs

            smooth_x = _smooth_in_corridor([w[0] for w in windows], *corridor(0, W), smoothing)
            smooth_y = _smooth_in_corridor([w[1] for w in windows], *corridor(1, H), smoothing)
            smoothed = []
            for i in range(B):
                crop_w, crop_h = sizes[i]
                smoothed.append((smooth_x[i], smooth_y[i], max(1, min(int(round(crop_w)), W)), max(1, min(int(round(crop_h)), H))))
            windows = smoothed

        # Step 4c: final output size. The shorter side is base_resolution; the
        # longer side follows the ratio (exact for e.g. 1080 at 16:9 ->
        # 1920x1080). Both are then snapped to the nearest multiple of
        # divisible_by (1 = no snapping).
        short_side = _round_to_multiple(base_resolution, divisible_by)
        long_side = _round_to_multiple(base_resolution * max(target_ratio, 1 / target_ratio), divisible_by)
        if target_ratio >= 1:
            target_width, target_height = long_side, short_side
        else:
            target_width, target_height = short_side, long_side

        # Step 5: crop + resize each frame, centered on that frame's own mask.
        image_list, mask_list, bbox_list = [], [], []
        for i in range(B):
            x_c, y_c, crop_w, crop_h = windows[i]
            x0 = int(round(x_c - crop_w / 2))
            y0 = int(round(y_c - crop_h / 2))
            b = boxes[i]
            if b is not None:
                # Rounding can shift a tight window by a pixel; snap it back so
                # the padded mask (exact pixel span) is fully inside.
                mask_x0 = int(round(b[0] - b[2] / 2 + 0.5))
                mask_y0 = int(round(b[1] - b[3] / 2 + 0.5))
                x0 = min(max(x0, mask_x0 + b[2] - crop_w), mask_x0)
                y0 = min(max(y0, mask_y0 + b[3] - crop_h), mask_y0)
            x0 = max(0, min(x0, W - crop_w))
            y0 = max(0, min(y0, H - crop_h))
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
