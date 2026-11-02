import math
import os

import torch
import torch.nn.functional as F
from PIL import Image

import folder_paths
import nodes as comfy_nodes

try:
    from nodes import MAX_RESOLUTION
except ImportError:
    MAX_RESOLUTION = 16384

CROP_DATA_TYPE = "CROP_DATA"
ASPECT_RATIO_CHOICES = ["free", "16:9", "4:3", "1:1", "3:4", "9:16", "21:9"]
PADDING_COLORS = {
    "grey": (0.5, 0.5, 0.5),
    "black": (0.0, 0.0, 0.0),
    "white": (1.0, 1.0, 1.0),
    "green": (0.0, 1.0, 0.0),
}
PASTE_CROP = "crop onto original"
PASTE_ORIGINAL = "original onto frame"
PREVIEW_MAX_SIDE = 1024

# Geometry shared by both nodes: a w x h frame centred at (cx, cy) in source
# pixels, with the image rotated by `angle` degrees (positive = clockwise on
# screen) behind the upright frame. The frame may extend past the image on any
# side; that area is the padding. An output pixel at offset d from the frame
# centre samples the source at c + R(-angle) d; compositing applies the inverse.


def _cos_sin(angle):
    a = math.radians(angle)
    return math.cos(a), math.sin(a)


def _frame_to_source(cx, cy, w, h, angle, out_w, out_h, device):
    """Source coordinates of every pixel of a w x h frame rendered at out_w x out_h."""
    cos_a, sin_a = _cos_sin(angle)
    xs = (torch.arange(out_w, device=device, dtype=torch.float32) + 0.5) * (w / out_w) - w / 2
    ys = (torch.arange(out_h, device=device, dtype=torch.float32) + 0.5) * (h / out_h) - h / 2
    dy, dx = torch.meshgrid(ys, xs, indexing="ij")
    return cx + cos_a * dx + sin_a * dy, cy - sin_a * dx + cos_a * dy


def _source_grid(sx, sy, src_w, src_h):
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


def _resize(images, h, w):
    if images.shape[1:3] == (h, w):
        return images
    return F.interpolate(images.movedim(-1, 1), size=(h, w), mode="bicubic",
                         align_corners=False).movedim(1, -1).clamp(0, 1)


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
    filename = f"imagereframe_preview_{node_id}.png"
    temp_dir = folder_paths.get_temp_directory()
    os.makedirs(temp_dir, exist_ok=True)
    pil.save(os.path.join(temp_dir, filename), compress_level=1)
    return {"filename": filename, "subfolder": "", "type": "temp"}


class ImageReframe:
    @classmethod
    def INPUT_TYPES(cls):
        required = dict(comfy_nodes.LoadImage.INPUT_TYPES()["required"])
        required.update({
            "center_x": ("FLOAT", {"default": 0.0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 0.5}),
            "center_y": ("FLOAT", {"default": 0.0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 0.5}),
            "frame_width": ("INT", {"default": 0, "min": 0, "max": MAX_RESOLUTION, "step": 1,
                                    "tooltip": "0 = full image"}),
            "frame_height": ("INT", {"default": 0, "min": 0, "max": MAX_RESOLUTION, "step": 1,
                                     "tooltip": "0 = full image"}),
            "rotation": ("FLOAT", {"default": 0.0, "min": -180.0, "max": 180.0, "step": 0.1,
                                   "display": "slider",
                                   "tooltip": "Rotates the image behind the frame, in degrees (positive = clockwise)"}),
            "aspect_ratio": (ASPECT_RATIO_CHOICES, {"default": "free",
                                                    "tooltip": "Locks the frame's proportions while dragging in the viewport"}),
            "padding_color": (list(PADDING_COLORS), {"default": "grey",
                                                     "tooltip": "Fill for the frame area outside the image"}),
        })
        return {
            "required": required,
            "optional": {
                "input_image": ("IMAGE", {"tooltip": "Replaces the uploaded image when connected (a single "
                                                     "image or a batch; the whole batch uses the same frame)"}),
                "mask": ("MASK", {"tooltip": "Cropped with the same frame and combined into the mask output"}),
                # Optional so saved API prompts without it still validate.
                "feathering": ("INT", {"default": 0, "min": 0, "max": MAX_RESOLUTION, "step": 1,
                                       "tooltip": "Fades the mask from the padding into the image over this many "
                                                  "pixels, along the image edges that border padding"}),
            },
            "hidden": {
                "node_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("IMAGE", "IMAGE", "MASK", CROP_DATA_TYPE)
    RETURN_NAMES = ("original_image", "cropped_image", "mask", "crop_data")
    OUTPUT_TOOLTIPS = (
        "The image as loaded (or as connected to input_image), uncropped",
        "The frame, with the padding filled with padding_color",
        "1 in the padded area; over the image, the cropped input mask (or 0), faded in by feathering",
        "The frame's geometry, for Image Reframe Composite",
    )
    FUNCTION = "reframe"
    CATEGORY = "image/transform"
    DESCRIPTION = (
        "Loads an image (or takes one from input_image) and reframes it: drag the frame in the "
        "viewport to crop, turn the image behind it with rotation, or push the frame past the "
        "image edges to pad the canvas for outpainting. The padded area is filled with "
        "padding_color and is 1 in the mask output. Pair with 'Image Reframe Composite' to paste "
        "a processed crop back onto the original, or the original back onto an outpainted result."
    )

    @classmethod
    def IS_CHANGED(cls, image, **kwargs):
        # Same lookup as Load Image, so hosts that fetch inputs on demand (Comfy
        # Cloud) see the file; a missing file only matters without input_image.
        try:
            return comfy_nodes.LoadImage.IS_CHANGED(image)
        except Exception:
            return ""

    @classmethod
    def VALIDATE_INPUTS(cls, image):
        # A missing file is only an error when nothing is connected to input_image,
        # which isn't known here; reframe() raises in that case.
        return True

    def reframe(self, image, center_x, center_y, frame_width, frame_height, rotation, aspect_ratio,
                padding_color, input_image=None, mask=None, feathering=0, node_id=None):
        # aspect_ratio only constrains the editor's drag behaviour in the frontend.
        if input_image is not None:
            source = input_image
        else:
            # No existence check first: Load Image's own lookup is what hosts
            # that fetch inputs on demand (Comfy Cloud) hook into.
            try:
                source = comfy_nodes.LoadImage().load_image(image)[0]
            except FileNotFoundError:
                raise ValueError(f"Invalid image file: {image}") from None
        source = source.float()
        batch, src_h, src_w, _ = source.shape
        device = source.device

        if frame_width <= 0 or frame_height <= 0:
            cx, cy, w, h = src_w / 2, src_h / 2, src_w, src_h
        else:
            cx, cy, w, h = center_x, center_y, frame_width, frame_height

        color = torch.tensor(PADDING_COLORS[padding_color], device=device)
        src_mask = None if mask is None else _normalize_mask(mask, batch, src_h, src_w).to(device)
        x0, y0 = cx - w / 2, cy - h / 2

        if rotation % 360 == 0 and float(x0).is_integer() and float(y0).is_integer():
            # Exact pixel copy: paste the overlapping part onto a filled canvas.
            x0, y0 = int(x0), int(y0)
            out_image = color.expand(batch, h, w, 3).clone()
            coverage = torch.zeros((h, w), device=device)
            crop_mask = torch.zeros((batch, h, w), device=device)
            ix0, iy0 = max(0, x0), max(0, y0)
            ix1, iy1 = min(src_w, x0 + w), min(src_h, y0 + h)
            if ix1 > ix0 and iy1 > iy0:
                out = (slice(None), slice(iy0 - y0, iy1 - y0), slice(ix0 - x0, ix1 - x0))
                out_image[out] = source[:, iy0:iy1, ix0:ix1, :3]
                coverage[out[1:]] = 1
                if src_mask is not None:
                    crop_mask[out] = src_mask[:, iy0:iy1, ix0:ix1]
        else:
            sx, sy = _frame_to_source(cx, cy, w, h, rotation, w, h, device)
            grid = _source_grid(sx, sy, src_w, src_h)
            # Sample with border padding and blend by coverage, so edges fade
            # into the padding colour instead of a dark fringe.
            sampled = _sample(source[..., :3].movedim(-1, 1), grid, "bicubic", "border").movedim(1, -1).clamp(0, 1)
            ones = torch.ones((1, 1, src_h, src_w), device=device)
            coverage = _sample(ones, grid, "bilinear", "zeros")[0, 0]
            out_image = sampled * coverage[None, ..., None] + color * (1 - coverage[None, ..., None])
            if src_mask is not None:
                crop_mask = _sample(src_mask.unsqueeze(1), grid, "bilinear", "border")[:, 0]

        # Over the image: the cropped input mask, raised by the feathering ramp.
        inner = None if src_mask is None else crop_mask
        if feathering > 0:
            # Same falloff as Pad Image for Outpainting: ((f - d) / f)^2 from the edge
            # pixel (0.81 at 10 px) to 0 at `feathering` px in, on edges that border padding.
            sx, sy = _frame_to_source(cx, cy, w, h, rotation, w, h, device)
            edges = [(sx, sx < 0), (src_w - sx, sx > src_w), (sy, sy < 0), (src_h - sy, sy > src_h)]
            distance = torch.full_like(sx, float("inf"))
            for dist, beyond in edges:
                if beyond.any():
                    distance = torch.minimum(distance, dist + 0.5)
            ramp = ((feathering - distance) / feathering).clamp(0, 1) ** 2
            inner = ramp if inner is None else torch.maximum(inner, ramp)

        # 1 in the padding; over the image, `inner` (or 0).
        if inner is None:
            out_mask = (1 - coverage).expand(batch, h, w).contiguous()
        else:
            out_mask = ((1 - coverage) + coverage * inner).expand(batch, h, w).contiguous()

        crop_data = {
            "source_width": src_w,
            "source_height": src_h,
            "center_x": float(cx),
            "center_y": float(cy),
            "width": int(w),
            "height": int(h),
            "angle": float(rotation),
        }

        ui = {"reframe_source_size": [src_w, src_h]}
        if input_image is not None:
            ui["reframe_preview"] = [_save_preview(source, node_id)]
        return {"ui": ui, "result": (source, out_image, out_mask, crop_data)}


class ImageReframeComposite:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "frame_image": ("IMAGE", {"tooltip": "An image in frame space at any resolution: the processed "
                                                     "crop, or the outpainted result"}),
                "original_image": ("IMAGE", {"tooltip": "The original, from Image Reframe's original_image "
                                                        "(or an edited copy of the same size)"}),
                "paste": ([PASTE_ORIGINAL, PASTE_CROP], {
                    "default": PASTE_ORIGINAL,
                    "tooltip": f"'{PASTE_ORIGINAL}': puts the original back onto frame_image (outpainting); "
                               f"output in frame space. '{PASTE_CROP}': puts frame_image back into the original, "
                               f"undoing the rotation; output in original space."}),
                "feather": ("INT", {"default": 0, "min": 0, "max": 1024, "step": 1,
                                    "tooltip": "Soft edge width in output pixels: inside the frame border when "
                                               "pasting the crop, inside the original's edges that border padding "
                                               "when pasting the original"}),
                "resize_source": ("BOOLEAN", {"default": False,
                                              "tooltip": "The source is the image being pasted (the original or the "
                                                         "crop, per paste). On: it is scaled to the other image's "
                                                         "resolution. Off: it keeps its pixels and the other image "
                                                         "is scaled to match."}),
            },
            # ComfyUI lists required inputs before optional ones, so crop_data is
            # declared optional to sit after mask; composite() requires it.
            "optional": {
                "mask": ("MASK", {"tooltip": "Limits the paste to this region (in frame space, any resolution)"}),
                "crop_data": (CROP_DATA_TYPE, {"tooltip": "From Image Reframe (required)"}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "composite"
    CATEGORY = "image/transform"
    DESCRIPTION = (
        "Pastes back across an 'Image Reframe' frame, either the processed crop into the original "
        "(undoing crop and rotation) or the original onto an outpainted result (undoing crop, "
        "padding and rotation), with optional feathering."
    )

    def composite(self, frame_image, original_image, paste, feather, resize_source, mask=None, crop_data=None):
        if crop_data is None:
            raise ValueError("Connect crop_data from an Image Reframe node.")
        original = original_image
        src_w, src_h = crop_data["source_width"], crop_data["source_height"]
        if (original.shape[2], original.shape[1]) != (src_w, src_h):
            raise ValueError(
                f"Original is {original.shape[2]}x{original.shape[1]}, but the frame was made from a "
                f"{src_w}x{src_h} image."
            )

        device = frame_image.device
        channels = frame_image.shape[3]
        batch = max(frame_image.shape[0], original.shape[0])
        frame_image = _match_batch(frame_image, batch)
        original = _match_batch(original, batch)[..., :channels].to(device=device, dtype=frame_image.dtype)

        if paste == PASTE_CROP:
            return (self._paste_crop(frame_image, original, crop_data, feather, resize_source, mask, batch),)
        return (self._paste_original(frame_image, original, crop_data, feather, resize_source, mask, batch),)

    def _paste_crop(self, crop, original, d, feather, resize_source, mask, batch):
        src_w, src_h = d["source_width"], d["source_height"]
        cx, cy, w, h = d["center_x"], d["center_y"], d["width"], d["height"]
        # Output in original space, scaled so the crop keeps its pixels unless resizing.
        kx, ky = (1.0, 1.0) if resize_source else (crop.shape[2] / w, crop.shape[1] / h)
        out_w, out_h = max(1, round(src_w * kx)), max(1, round(src_h * ky))
        kx, ky = out_w / src_w, out_h / src_h
        out = _resize(original, out_h, out_w).clone()

        cos_a, sin_a = _cos_sin(d["angle"])
        corners_x = [cx + cos_a * dx + sin_a * dy for dx in (-w / 2, w / 2) for dy in (-h / 2, h / 2)]
        corners_y = [cy - sin_a * dx + cos_a * dy for dx in (-w / 2, w / 2) for dy in (-h / 2, h / 2)]
        x0 = max(0, math.floor(min(corners_x) * kx))
        x1 = min(out_w, math.ceil(max(corners_x) * kx))
        y0 = max(0, math.floor(min(corners_y) * ky))
        y1 = min(out_h, math.ceil(max(corners_y) * ky))
        if x1 <= x0 or y1 <= y0:
            return out

        device = crop.device
        px = (torch.arange(x0, x1, device=device, dtype=torch.float32) + 0.5) / kx - cx
        py = (torch.arange(y0, y1, device=device, dtype=torch.float32) + 0.5) / ky - cy
        py, px = torch.meshgrid(py, px, indexing="ij")
        dx = cos_a * px - sin_a * py
        dy = sin_a * px + cos_a * py
        grid = torch.stack((dx / (w / 2), dy / (h / 2)), dim=-1).unsqueeze(0)

        edge_distance = torch.minimum(w / 2 - dx.abs(), h / 2 - dy.abs()) * min(kx, ky)
        if feather > 0:
            alpha = (edge_distance / feather).clamp(0, 1)
        else:
            alpha = (edge_distance + 0.5).clamp(0, 1)
        alpha = alpha.expand(batch, -1, -1)

        if mask is not None:
            m = _normalize_mask(mask, batch, mask.shape[-2], mask.shape[-1]).to(device)
            alpha = alpha * _sample(m.unsqueeze(1), grid, "bilinear", "border")[:, 0]

        pasted = _sample(crop.movedim(-1, 1), grid, "bicubic", "border").movedim(1, -1).clamp(0, 1)
        alpha = alpha.unsqueeze(-1)
        region = out[:, y0:y1, x0:x1, :]
        out[:, y0:y1, x0:x1, :] = region * (1 - alpha) + pasted * alpha
        return out

    def _paste_original(self, frame_image, original, d, feather, resize_source, mask, batch):
        src_w, src_h = d["source_width"], d["source_height"]
        w, h = d["width"], d["height"]
        device = frame_image.device

        if resize_source:
            out_h, out_w = frame_image.shape[1], frame_image.shape[2]
        else:
            out_h, out_w = h, w
            frame_image = _resize(frame_image, h, w)

        sx, sy = _frame_to_source(d["center_x"], d["center_y"], w, h, d["angle"], out_w, out_h, device)
        grid = _source_grid(sx, sy, src_w, src_h)
        pasted = _sample(original.movedim(-1, 1), grid, "bicubic", "border").movedim(1, -1).clamp(0, 1)

        # Distance inside the original to each edge that borders padding in this
        # frame, in output pixels; edges cut off by the frame stay hard.
        k = min(out_w / w, out_h / h)
        edges = [(sx, sx < 0), (src_w - sx, sx > src_w), (sy, sy < 0), (src_h - sy, sy > src_h)]
        distance = torch.full_like(sx, float("inf"))
        for dist, beyond in edges:
            if beyond.any():
                distance = torch.minimum(distance, dist * k)
        inside = ((sx >= 0) & (sx <= src_w) & (sy >= 0) & (sy <= src_h)).float()
        if feather > 0:
            alpha = (distance / feather).clamp(0, 1) * inside
        else:
            alpha = (distance + 0.5).clamp(0, 1) * inside
        alpha = alpha.expand(batch, -1, -1)

        if mask is not None:
            alpha = alpha * _normalize_mask(mask, batch, out_h, out_w).to(device)

        alpha = alpha.unsqueeze(-1)
        return frame_image * (1 - alpha) + pasted * alpha


NODE_CLASS_MAPPINGS = {
    "ImageReframe": ImageReframe,
    "ImageReframeComposite": ImageReframeComposite,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ImageReframe": "Image Reframe",
    "ImageReframeComposite": "Image Reframe Composite",
}
