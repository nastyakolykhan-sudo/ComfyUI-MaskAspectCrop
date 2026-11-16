"""Optional Image: tells whether an optional image input was given, without running an empty upload.

An upload node with no file (an app input the user left empty) fails as soon as it runs. This node looks at the
queued prompt instead: if its `image` socket is fed by an upload node whose file field is empty, it reports
`given = false` and never asks for the image, so the upload node and everything above it are skipped. Wire `given`
into a lazy switch (If/Else Switch) to pick the branch with or without the image.
"""

# Upload nodes and the input that holds their file name. Any other source counts as given.
_UPLOAD_NODES = {
    "WorkflowIOImageInput": "value",
    "LoadImage": "image",
    "LoadImageMask": "image",
    "LoadImageOutput": "image",
    "ImageReframe": "image",
}


def image_given(prompt, unique_id, name="image"):
    """Will input `name` of node `unique_id` deliver an image? False when it is unlinked or fed straight by an
    upload node with an empty file field."""
    node = (prompt or {}).get(str(unique_id), {})
    link = node.get("inputs", {}).get(name)
    if not isinstance(link, (list, tuple)) or not link:
        return False
    src = (prompt or {}).get(str(link[0]), {})
    key = _UPLOAD_NODES.get(src.get("class_type"))
    if key is None:
        return True
    src_inputs = src.get("inputs", {})
    if src.get("class_type") == "ImageReframe" and isinstance(src_inputs.get("input_image"), (list, tuple)):
        return True  # Image Reframe fed by another node: that node decides, not the empty file widget
    return bool(str(src_inputs.get(key, "") or "").strip())


class OptionalImage:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {
                "image": ("IMAGE", {"lazy": True, "tooltip": "The optional image, usually straight from an upload node."}),
            },
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE", "BOOLEAN")
    RETURN_NAMES = ("image", "given")
    OUTPUT_TOOLTIPS = (
        "The image when one was given, otherwise nothing: only use it behind a switch on `given`.",
        "True when an image was given. Feed it to an If/Else Switch.",
    )
    FUNCTION = "run"
    CATEGORY = "image"
    DESCRIPTION = ("Tells whether an optional image was given. An empty upload is never run, so it can't fail; "
                   "switch on `given` to take the branch with or without the image.")

    def check_lazy_status(self, prompt=None, unique_id=None, image=None):
        if image is None and image_given(prompt, unique_id):
            return ["image"]
        return []

    def run(self, prompt=None, unique_id=None, image=None):
        given = image is not None
        print(f"[Optional Image] given: {given}")
        return (image, given)


NODE_CLASS_MAPPINGS = {"OptionalImage": OptionalImage}
NODE_DISPLAY_NAME_MAPPINGS = {"OptionalImage": "Optional Image (Given?)"}
