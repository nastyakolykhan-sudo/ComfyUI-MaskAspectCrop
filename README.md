# ComfyUI-MaskAspectCrop

Crop-and-paste-back tools for images and video batches:

- **Image Crop By Mask To Aspect Ratio**: a batch-aware take on KJNodes'
  `Image Crop By Mask And Resize`, built for video (a batch of images + a batch of
  masks where the mask moves/changes over time) and for a fixed set of allowed output
  aspect ratios instead of an arbitrary one. Paste back with KJNodes'
  `Image Uncrop By Mask`.
- **Crop & Rotate** + **Crop & Rotate Composite**: interactively crop a *rotated*
  region, process it, and paste it back exactly where it came from. See
  [Crop & Rotate](#crop--rotate) below.

## Install

```bash
cd ComfyUI/custom_nodes
git clone <this repo>
```

No extra Python dependencies. Restart ComfyUI.

# Image Crop By Mask To Aspect Ratio

## Why not just use KJNodes' node directly

`Image Crop By Mask And Resize` derives its own aspect ratio from the mask's bounding
box, then computes the crop size and the final resize size independently (each rounded
to a multiple of 16 separately) — so the two can end up at very slightly different
ratios, which shows up as visible stretching on some inputs. It also has no way to
constrain the output to one of a fixed set of ratios (e.g. the ones your downstream
model or template actually supports).

## What this node does

1. For every frame in the batch, finds the mask's bounding box and expands it by
   `padding` (clamped so it never exceeds the frame).
2. Takes the largest width and largest height across the whole batch — the "envelope"
   the crop has to cover everywhere, since the mask can move or change size over time.
3. Picks the target aspect ratio for the *whole batch*:
   - a fixed one you choose (`16:9`, `4:3`, `1:1`, `3:4`, `9:16`, `21:9`), or
   - `adaptive`: whichever of those six is numerically closest to the envelope's own
     ratio.
4. Builds the crop window(s), depending on `crop_mode`:
   - `uniform`: **one** window, shared by every frame, sized to fully contain the
     batch's envelope (from step 2) at the target ratio.
   - `tight`: **each frame gets its own window**, sized to fully contain just *that
     frame's* own mask (from step 1) at the target ratio.
   Either way, the window already has the target ratio (expanding the shorter side
   rather than stretching), clamped to the source image bounds.
5. Centers each frame's window on that frame's own mask bbox center (so the crop
   still tracks a moving mask), crops, then resizes every frame to one
   `base_resolution`-driven output size (shorter side = `base_resolution`, rounded to
   a multiple of 16). Because each crop window is already shaped to the target ratio,
   this resize is a uniform scale in both dimensions — no distortion.

### `tight` vs `uniform`

Both give margin-free output *when the mask's own bbox already matches the target
ratio* — the margin you otherwise see is the unavoidable cost of forcing a specific
ratio without stretching. Where they differ is the batch:

- `tight` — every frame is cropped as closely as possible to its own mask. Minimal
  margin, always. But since every frame still gets resized to the same output
  resolution, a frame with a smaller mask ends up more "zoomed in" than one with a
  larger mask — the effective scale can vary frame to frame, which for video can read
  as subtle zoom/breathing even though nothing in the scene changed size.
- `uniform` — every frame uses the same crop window size (only the position tracks
  the mask), so the zoom level is constant across the whole clip. The cost is margin
  on any frame whose mask is smaller than the batch's largest.

Default is `tight`. Switch to `uniform` if you see zoom jitter in the output video and
stable framing matters more than tight cropping.

## Inputs

| Input | Type | Default | Notes |
|---|---|---|---|
| `image` | IMAGE | — | Batch of frames |
| `mask` | MASK | — | Matching batch of masks |
| `aspect_ratio` | COMBO | `adaptive` | `16:9`, `4:3`, `1:1`, `3:4`, `9:16`, `21:9`, or `adaptive` |
| `crop_mode` | COMBO | `tight` | `tight` (per-frame window, minimal margin) or `uniform` (one window for the whole batch, stable zoom) — see below |
| `base_resolution` | INT | 512 | Shorter side of the output, in pixels |
| `padding` | INT | 0 | Extra margin added around the mask bbox before fitting |

## Outputs

| Output | Type | Notes |
|---|---|---|
| `images` | IMAGE | Cropped + resized batch |
| `masks` | MASK | Cropped + resized batch, matching `images` |
| `bbox` | BBOX | Per-frame `(x0, y0, x1, y1)` in the *original* image — feeds directly into KJNodes' `Image Uncrop By Mask` |
| `aspect_ratio` | COMBO | The resolved ratio actually used (e.g. `adaptive` always resolves to one of the six concrete values here, never `"adaptive"` itself) |

## Notes

- `bbox` uses the same type string (`BBOX,BOUNDING_BOX`) and xyxy tuple format as
  KJNodes' `Image Crop By Mask And Resize`, so it's a drop-in replacement anywhere you
  were already pairing that node with `Image Uncrop By Mask`.
- Unlike KJNodes' node, this one has no `min_crop_resolution`/`max_crop_resolution`
  clamps — the aspect-ratio fitting in step 4 replaces that role.
- "Closest" ratio (for `adaptive`) is measured on a log scale, so e.g. `2:1` and `1:2`
  are equally "far" from `1:1` — orientation doesn't bias the pick.

# Crop & Rotate

Crop a **rotated** region out of an image (or a whole video batch), process it, and
paste it back exactly where it came from. The image turns behind an upright frame
(Lightroom-style), so what you see in the frame is exactly what comes out. Works the
same for a single image (a batch of one).

## Crop & Rotate node

1. Connect an image (or video frame batch) and run the workflow once — this loads the
   first frame into the node's preview.
2. In the preview:
   - drag inside the frame to move it, drag the edges/corners to resize,
     drag outside the frame to draw a new one;
   - use the **rotation** slider to turn the image behind the frame (positive =
     clockwise). The frame stays locked to the same content while you rotate.
   - **aspect_ratio** locks the frame's proportions while dragging (and reshapes the
     current frame when you change it). **Reset** puts everything back to defaults: full image, 0° rotation, `free`
     aspect.
3. Run again to apply.

The frame can extend past the image edges once rotated. Those areas are filled black in
the output image and are `0` in the output mask.

| Input | Type | Notes |
|---|---|---|
| `image` | IMAGE | Single image or batch — the whole batch uses the same frame |
| `mask` | MASK | Optional — cropped with the exact same geometry |
| `center_x`, `center_y` | FLOAT | Frame centre, in source pixels (set by dragging) |
| `crop_width`, `crop_height` | INT | Frame size in source pixels; `0` = full image |
| `rotation` | FLOAT | Degrees, −180…180, positive = clockwise |
| `aspect_ratio` | COMBO | `free`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16`, `21:9` — editor drag constraint only |

| Output | Type | Notes |
|---|---|---|
| `image` | IMAGE | The straightened crop, at native resolution (`crop_width × crop_height`) |
| `mask` | MASK | The input mask cropped the same way; without a mask input, `1` where the crop covers the source and `0` in the filled corners |
| `crop_rotate` | CROP_ROTATE | Frame + rotation data for the composite node |

With rotation at `0` and the frame inside the image, the crop is an exact pixel slice
(no resampling).

## Crop & Rotate Composite node

| Input | Type | Notes |
|---|---|---|
| `destination` | IMAGE | The original image/batch that was cropped (must be the same size) |
| `source` | IMAGE | The processed crop — any resolution; it's mapped onto the original frame |
| `crop_rotate` | CROP_ROTATE | From the Crop & Rotate node |
| `feather` | INT | Soft edge inside the frame border, in destination pixels |
| `mask` | MASK | Optional — paste only this region (in crop space, any resolution) |

Only the rotated frame area of the destination is touched; everything outside it is
left bit-identical. Single images and batches broadcast against each other (e.g. one
processed still pasted onto every frame of a video).

## Crop & Rotate notes

- The preview shows the first frame of the batch. If the input resolution changes, the
  crop resets to the full image and asks you to run again.
- One frame for the whole batch — there's no per-frame keyframing of the crop.
- The composite needs the destination at the same size the crop was taken from; it
  errors clearly otherwise rather than guessing a rescale.
