# ComfyUI-MaskAspectCrop

Crop-and-paste-back tools for images and video batches:

- **Image Crop By Mask To Aspect Ratio**: a batch-aware take on KJNodes'
  `Image Crop By Mask And Resize`, built for video (a batch of images + a batch of
  masks where the mask moves/changes over time) and for a fixed set of allowed output
  aspect ratios instead of an arbitrary one. Paste back with KJNodes'
  `Image Uncrop By Mask`.
- **Image Reframe** + **Image Reframe Composite**: a Load Image node with a viewport
  for cropping, rotating, or padding the canvas for outpainting, plus a composite that
  pastes a processed crop back into the original, or the original back onto an
  outpainted result. See [Image Reframe](#image-reframe) below.

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
   Frames with an empty mask are left out.
3. Picks the target aspect ratio for the *whole batch*:
   - a fixed one you choose (`16:9`, `4:3`, `1:1`, `3:4`, `9:16`, `21:9`), or
   - `adaptive`: whichever of those six is numerically closest to the envelope's own
     ratio (or to the image's own ratio when no frame has a mask).
4. Handles frames with an empty mask (see [Frames without a mask](#frames-without-a-mask)).
5. Builds the crop window(s), depending on `crop_mode`:
   - `uniform`: **one** window, shared by every frame, sized to fully contain the
     batch's envelope (from step 2) at the target ratio.
   - `tight`: **each frame gets its own window**, sized to fully contain just *that
     frame's* own mask (from step 1) at the target ratio.
   Either way, the window already has the target ratio (expanding the shorter side
   rather than stretching), clamped to the source image bounds.
6. Centers each frame's window on that frame's own mask bbox center (so the crop
   still tracks a moving mask), then optionally smooths the windows over time (see
   [Smoothing](#smoothing)).
7. Crops, then resizes every frame to one `base_resolution`-driven output size
   (shorter side = `base_resolution`, longer side = that times the ratio, both rounded
   to the nearest multiple of `divisible_by`, which defaults to `1` = exact). Because
   each crop window is already shaped to the target ratio, this resize is a uniform
   scale in both dimensions — no distortion.

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
stable framing matters more than tight cropping, or keep `tight` and turn on
`smoothing`.

### Frames without a mask

`empty_mask` decides what a frame with an empty mask shows:

- `hold_last` (default): reuses the crop of the last frame that had a mask, so the
  framing simply freezes while the mask is gone. Empty frames at the very start of the
  clip take the first frame that has a mask.
- `full_image`: uses the whole image. With a ratio that matches the image (e.g.
  `16:9` on a 1920×1080 source) that's exactly the full frame, uncropped; with a
  different forced ratio it's the largest centred window of that ratio, since a true
  full frame isn't possible without stretching.

A batch (or single image) with no mask at all always uses the whole image, and
`adaptive` then picks the ratio closest to the image itself.

### Smoothing

`smoothing` (in frames, `0` = off) evens out jumps in the crop's position and zoom
across the clip, like a stabilised camera following the mask:

- The motion is averaged over roughly that many frames on either side, with no lag
  (the whole clip is analysed at once), and steady movement isn't slowed down at the
  start or end of the clip.
- The mask is never cut off: each frame's window is never smaller than that frame's
  own window, and its position is kept within the range where that frame's padded
  mask still fits inside. A window that's about to grow starts growing a little early
  instead.
- With `full_image`, the zoom out to the full frame and back eases in and out instead
  of snapping.

Start around `3`–`8` for typical mask jitter; higher values give a steadier, slower
camera, at the cost of a little more margin around the mask.

## Inputs

| Input | Type | Default | Notes |
|---|---|---|---|
| `image` | IMAGE | — | Batch of frames |
| `mask` | MASK | — | Matching batch of masks |
| `aspect_ratio` | COMBO | `adaptive` | `16:9`, `4:3`, `1:1`, `3:4`, `9:16`, `21:9`, or `adaptive` |
| `crop_mode` | COMBO | `tight` | `tight` (per-frame window, minimal margin) or `uniform` (one window for the whole batch, stable zoom) — see below |
| `base_resolution` | INT | 512 | Shorter side of the output, in pixels |
| `padding` | INT | 0 | Extra margin added around the mask bbox before fitting |
| `divisible_by` | INT | 1 | Snaps both output sides to the nearest multiple of this. `1` = exact `base_resolution` (1080 at 16:9 → 1920×1080); `16` for models that need it (1080 → 1088×1920 at 9:16) |
| `empty_mask` | COMBO | `hold_last` | What frames with an empty mask show: `hold_last` or `full_image` — see [Frames without a mask](#frames-without-a-mask) |
| `smoothing` | INT | 0 | Smooths the crop's movement and zoom over time, in frames (`0` = off) — see [Smoothing](#smoothing) |

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

# Image Reframe

Load an image and reframe it in one node: crop it, turn it behind the frame, or push
the frame past the image edges to pad the canvas for outpainting. Then paste back,
either the processed crop into the original or the original onto the outpainted
result, so the untouched part keeps its exact pixels. Works on a single image or a
whole video batch.

## Image Reframe node

Replaces the native **Load Image** node: pick or upload an image (or drag one onto
the node) and it shows in the node's viewport straight away, no run needed. To take
an image from another node instead (e.g. video frames), connect it to `input_image`;
the viewport then shows the first frame after one run.

In the viewport:

- drag inside the frame to move it, drag the edges/corners to resize, drag outside
  the frame to draw a new one. The frame can go past the image on any side (up to the
  image's longer side again in each direction); that area is the padding, shown in
  `padding_color`;
- **rotation** turns the image behind the upright frame (positive = clockwise);
- **aspect_ratio** locks the frame's proportions while dragging (and reshapes the
  current frame when you change it);
- **Reset** restores the full image, 0° and `free`; it leaves the image and
  `padding_color` as they are.

A new image of a different size resets the frame to the full image.

| Input | Type | Notes |
|---|---|---|
| `image` | COMBO + upload | As in Load Image (files in ComfyUI's `input` folder) |
| `center_x`, `center_y` | FLOAT | Frame centre, in source pixels (set by dragging) |
| `frame_width`, `frame_height` | INT | Frame size in source pixels; `0` = full image |
| `rotation` | FLOAT | Degrees, −180…180, positive = clockwise |
| `aspect_ratio` | COMBO | `free`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16`, `21:9` — editor drag constraint only |
| `padding_color` | COMBO | `grey` (50%), `black`, `white`, `green` (#00FF00) |
| `input_image` | IMAGE | Optional — replaces the uploaded image; a batch uses one frame for all frames |
| `mask` | MASK | Optional — cropped with the same frame and merged into the mask output |

| Output | Type | Notes |
|---|---|---|
| `original_image` | IMAGE | The image as loaded (or as connected to `input_image`), uncropped |
| `cropped_image` | IMAGE | The frame at native resolution (`frame_width × frame_height`), padding filled with `padding_color` |
| `mask` | MASK | `1` in the padded area; over the image, the cropped input `mask` (or `0`) — ready for outpainting |
| `crop_data` | CROP_DATA | The frame's geometry (size, centre, rotation), for the composite node |

With rotation at `0` and whole-pixel frame edges, the image part is an exact pixel copy
(no resampling). Animated files load all frames as a batch, like Load Image; the
image's alpha channel is ignored.

## Image Reframe Composite node

| Input | Type | Notes |
|---|---|---|
| `frame_image` | IMAGE | An image in frame space, any resolution: the processed crop or the outpainted result |
| `original_image` | IMAGE | From Image Reframe's `original_image` (or an edited copy of the same size) |
| `mask` | MASK | Optional — paste only this region (frame space, any resolution) |
| `crop_data` | CROP_DATA | From the Image Reframe node (required; listed after `mask` so the sockets read images, mask, data) |
| `paste` | COMBO | `original onto frame` or `crop onto original` (see below) |
| `feather` | INT | Soft edge width, in output pixels |
| `resize_source` | BOOLEAN | See below |

`paste`:

- **original onto frame** (default, for outpainting): puts the original back onto
  `frame_image`, undoing crop, padding and rotation. Output is in frame space.
  Feathering runs only along the original's edges that border padding; edges the frame
  cuts through stay hard, since nothing was generated there.
- **crop onto original**: puts `frame_image` back into the original, undoing the
  rotation. Output is in original space; only the frame area is touched, everything
  else is left bit-identical. Feathering runs inside the frame border.

`resize_source` (as in the native Image Composite Masked, the source is the image being
pasted: the original in `original onto frame`, `frame_image` in `crop onto original`):

- **off** (default): the pasted image keeps its pixels and the destination is scaled to
  match. Pasting the original gives an output at the frame's size; pasting a crop
  processed at 2× gives the original at 2×.
- **on**: the pasted image is scaled to the destination's resolution, so the output
  keeps the destination's size.

Single images and batches broadcast against each other (e.g. one processed still
pasted onto every frame of a video).

