# ComfyUI-MaskAspectCrop

One node: **Image Crop By Mask To Aspect Ratio**. It's a batch-aware take on KJNodes'
`Image Crop By Mask And Resize`, built for video (a batch of images + a batch of masks
where the mask moves/changes over time) and for a fixed set of allowed output aspect
ratios instead of an arbitrary one.

## Install

```bash
cd ComfyUI/custom_nodes
git clone <this repo>
```

No extra Python dependencies. Restart ComfyUI.

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
3. Picks the target aspect ratio:
   - a fixed one you choose (`16:9`, `4:3`, `1:1`, `3:4`, `9:16`, `21:9`), or
   - `adaptive`: whichever of those six is numerically closest to the envelope's own
     ratio.
4. Builds one crop window, shared by the whole batch, that already has the target
   aspect ratio and fully contains the envelope (expanding the shorter side rather
   than stretching), clamped to the source image bounds.
5. Centers that same-size window on each frame's own mask bbox center (so the crop
   still tracks a moving mask), crops, then resizes every frame to one
   `base_resolution`-driven output size (shorter side = `base_resolution`, rounded to
   a multiple of 16). Because the crop window is already shaped to the target ratio,
   this resize is a uniform scale in both dimensions — no distortion.

## Inputs

| Input | Type | Default | Notes |
|---|---|---|---|
| `image` | IMAGE | — | Batch of frames |
| `mask` | MASK | — | Matching batch of masks |
| `aspect_ratio` | COMBO | `adaptive` | `16:9`, `4:3`, `1:1`, `3:4`, `9:16`, `21:9`, or `adaptive` |
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
