import math
import os

import torch
import torch.nn.functional as F
from PIL import Image

import folder_paths

try:
    from nodes import MAX_RESOLUTION
except ImportError:
    MAX_RESOLUTION = 16384

CROP_ROTATE_TYPE = "CROP_ROTATE"
ASPECT_RATIO_CHOICES = ["free", "16:9", "4:3", "1:1", "3:4", "9:16", "21:9"]
PREVIEW_MAX_SIDE = 1024

# Geometry shared by both nodes: a w x h frame centred at (cx, cy) in source
# pixels, with the image rotated by `angle` degrees (positive = clockwise on
# screen) behind the upright frame. An output pixel at offset d from the frame
# centre samples the source at c + R(-angle) d; compositing applies the inverse.


def _cos_sin(angle):
    a = math.radians(angle)
    return math.cos(a), math.sin(a)


def _crop_grid(cx, cy, w, h, angle, src_w, src_h, device):
    cos_a, sin_a = _cos_sin(angle)
    xs = torch.arange(w, device=device, dtype=torch.float32) + 0.5 - w / 2
    ys = torch.arange(h, device=device, dtype=torch.float32) + 0.5 - h / 2
    dy, dx = torch.meshgrid(ys, xs, indexing="ij")
    sx = cx + cos_a * dx + sin_a * dy
    sy = cy - sin_a * dx + cos_a * dy
    return torch.stack((sx / src_w * 2 - 1, sy / src_h * 2 - 1), dim=-1).unsqueeze(0)


def _sample(batch_chw, grid, mode, padding_mode):
    """grid_sample a (B, C, H, W) tensor with one shared grid by folding the
    batch into channels, so the grid never has to be repeated per frame."""
    b, c, h, w = batch_chw.shape
    out = F.grid_sample(
        batch_chw.reshape(1, b * c, h, w), grid,
        mode=mode, padding_mode=padding_mode, align_corners=False,
    )
    return out.reshape(b, c, grid.shape[1], grid.shape[2])


def _match_batch(t, batch_size):
    if t.shape[0] == batch_size:
        return t
    if t.shape[0] == 1:
        return t.expand(batch_size, *t.shape[1:])
    raise ValueError(f"Batch size mismatch: got {t.shape[0]}, expected {batch_size} (or 1).")


def _normalize_mask(mask, batch_size, h, w):
    if mask.dim() == 4 and mask.shape[1] == 1:
        mask = mask.squeeze(1)
    elif mask.dim() == 2:
        mask = mask.unsqueeze(0)
    mask = _match_batch(mask.float(), batch_size)
    if mask.shape[1:] != (h, w):
        mask = F.interpolate(mask.unsqueeze(1), size=(h, w), mode="bilinear", align_corners=False).squeeze(1)
    return mask


def _save_preview(image, node_id):
    frame = (image[0, :, :, :3].clamp(0, 1) * 255).byte().cpu().numpy()
    pil = Image.fromarray(frame)
    pil.thumbnail((PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE))
    filename = f"croprotate_preview_{node_id}.png"
    temp_dir = folder_paths.get_temp_directory()
    os.makedirs(temp_dir, exist_ok=True)
    pil.save(os.path.join(temp_dir, filename), compress_level=1)
    return {"filename": filename, "subfolder": "", "type": "temp"}


class CropRotate:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "center_x": ("FLOAT", {"default": 0.0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 0.5}),
                "center_y": ("FLOAT", {"default": 0.0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 0.5}),
                "crop_width": ("INT", {"default": 0, "min": 0, "max": MAX_RESOLUTION, "step": 1,
                                       "tooltip": "0 = full image"}),
                "crop_height": ("INT", {"default": 0, "min": 0, "max": MAX_RESOLUTION, "step": 1,
                                        "tooltip": "0 = full image"}),
                "rotation": ("FLOAT", {"default": 0.0, "min": -180.0, "max": 180.0, "step": 0.1,
                                       "display": "slider",
                                       "tooltip": "Rotates the image behind the crop frame, in degrees (positive = clockwise)"}),
                "aspect_ratio": (ASPECT_RATIO_CHOICES, {"default": "free",
                                                        "tooltip": "Locks the crop frame's proportions while dragging in the preview"}),
            },
            "optional": {
                "mask": ("MASK",),
            },
            "hidden": {
                "node_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("IMAGE", "MASK", CROP_ROTATE_TYPE)
    RETURN_NAMES = ("image", "mask", "crop_rotate")
    FUNCTION = "crop"
    CATEGORY = "image/transform"
    DESCRIPTION = (
        "Crops a rotated region of the image (the whole batch uses the same frame). Run once "
        "to load the preview, then drag in the preview to set the frame and use the rotation "
        "slider to turn the image behind it. Areas outside the source are filled black and are "
        "0 in the mask output. Pair with 'Crop & Rotate Composite' to paste the result back."
    )

    def crop(self, image, center_x, center_y, crop_width, crop_height, rotation, aspect_ratio,
             mask=None, node_id=None):
        # aspect_ratio only constrains the editor's drag behaviour in the frontend.
        batch, src_h, src_w, _ = image.shape

        if crop_width <= 0 or crop_height <= 0:
            cx, cy, w, h = src_w / 2, src_h / 2, src_w, src_h
        else:
            cx, cy, w, h = center_x, center_y, crop_width, crop_height

        x0, y0 = cx - w / 2, cy - h / 2
        axis_aligned_inside = (
            rotation % 360 == 0
            and float(x0).is_integer() and float(y0).is_integer()
            and x0 >= 0 and y0 >= 0 and x0 + w <= src_w and y0 + h <= src_h
        )

        if axis_aligned_inside:
            x0, y0 = int(x0), int(y0)
            out_image = image[:, y0:y0 + h, x0:x0 + w, :]
            if mask is None:
                out_mask = torch.ones((batch, h, w), dtype=torch.float32, device=image.device)
            else:
                out_mask = _normalize_mask(mask, batch, src_h, src_w)[:, y0:y0 + h, x0:x0 + w]
        else:
            grid = _crop_grid(cx, cy, w, h, rotation, src_w, src_h, image.device)
            out_image = _sample(image.movedim(-1, 1), grid, "bicubic", "zeros").movedim(1, -1).clamp(0, 1)
            if mask is None:
                ones = torch.ones((1, 1, src_h, src_w), dtype=torch.float32, device=image.device)
                out_mask = _sample(ones, grid, "bilinear", "zeros")[:, 0].expand(batch, h, w)
            else:
                m = _normalize_mask(mask, batch, src_h, src_w).to(image.device)
                out_mask = _sample(m.unsqueeze(1), grid, "bilinear", "zeros")[:, 0]

        crop_rotate = {
            "source_width": src_w,
            "source_height": src_h,
            "center_x": float(cx),
            "center_y": float(cy),
            "width": int(w),
            "height": int(h),
            "angle": float(rotation),
        }

        return {
            "ui": {
                "crop_rotate_preview": [_save_preview(image, node_id)],
                "crop_rotate_source_size": [src_w, src_h],
            },
            "result": (out_image, out_mask.contiguous(), crop_rotate),
        }


class CropRotateComposite:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "destination": ("IMAGE", {"tooltip": "The original image(s) that were cropped"}),
                "source": ("IMAGE", {"tooltip": "The processed crop, at any resolution"}),
                "crop_rotate": (CROP_ROTATE_TYPE,),
                "feather": ("INT", {"default": 0, "min": 0, "max": 1024, "step": 1,
                                    "tooltip": "Soft edge width inside the crop frame, in destination pixels"}),
            },
            "optional": {
                "mask": ("MASK", {"tooltip": "Limits the paste to this region (in crop space, any resolution)"}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "composite"
    CATEGORY = "image/transform"
    DESCRIPTION = (
        "Pastes a crop made by 'Crop & Rotate' back onto the original, undoing the rotation. "
        "The source can be any resolution; it is mapped onto the original crop frame."
    )

    def composite(self, destination, source, crop_rotate, feather, mask=None):
        dst_h, dst_w, channels = destination.shape[1], destination.shape[2], destination.shape[3]
        if (dst_w, dst_h) != (crop_rotate["source_width"], crop_rotate["source_height"]):
            raise ValueError(
                f"Destination is {dst_w}x{dst_h}, but the crop was taken from a "
                f"{crop_rotate['source_width']}x{crop_rotate['source_height']} image."
            )

        batch = max(destination.shape[0], source.shape[0])
        destination = _match_batch(destination, batch)
        source = _match_batch(source, batch)[..., :channels].to(destination.device)

        cx, cy = crop_rotate["center_x"], crop_rotate["center_y"]
        w, h = crop_rotate["width"], crop_rotate["height"]
        cos_a, sin_a = _cos_sin(crop_rotate["angle"])

        corners_x = [cx + cos_a * dx + sin_a * dy for dx in (-w / 2, w / 2) for dy in (-h / 2, h / 2)]
        corners_y = [cy - sin_a * dx + cos_a * dy for dx in (-w / 2, w / 2) for dy in (-h / 2, h / 2)]
        x0 = max(0, math.floor(min(corners_x)))
        x1 = min(dst_w, math.ceil(max(corners_x)))
        y0 = max(0, math.floor(min(corners_y)))
        y1 = min(dst_h, math.ceil(max(corners_y)))
        if x1 <= x0 or y1 <= y0:
            return (destination.clone(),)

        device = destination.device
        px = torch.arange(x0, x1, device=device, dtype=torch.float32) + 0.5 - cx
        py = torch.arange(y0, y1, device=device, dtype=torch.float32) + 0.5 - cy
        py, px = torch.meshgrid(py, px, indexing="ij")
        dx = cos_a * px - sin_a * py
        dy = sin_a * px + cos_a * py
        grid = torch.stack((dx / (w / 2), dy / (h / 2)), dim=-1).unsqueeze(0)

        edge_distance = torch.minimum(w / 2 - dx.abs(), h / 2 - dy.abs())
        if feather > 0:
            alpha = (edge_distance / feather).clamp(0, 1)
        else:
            alpha = (edge_distance + 0.5).clamp(0, 1)
        alpha = alpha.expand(batch, -1, -1)

        if mask is not None:
            m = _normalize_mask(mask, batch, mask.shape[-2], mask.shape[-1]).to(device)
            alpha = alpha * _sample(m.unsqueeze(1), grid, "bilinear", "border")[:, 0]

        pasted = _sample(source.movedim(-1, 1), grid, "bicubic", "border").movedim(1, -1).clamp(0, 1)
        alpha = alpha.unsqueeze(-1)

        out = destination.clone()
        region = out[:, y0:y1, x0:x1, :]
        out[:, y0:y1, x0:x1, :] = region * (1 - alpha) + pasted * alpha
        return (out,)


NODE_CLASS_MAPPINGS = {
    "CropRotate": CropRotate,
    "CropRotateComposite": CropRotateComposite,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CropRotate": "Crop & Rotate",
    "CropRotateComposite": "Crop & Rotate Composite",
}
