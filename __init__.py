from . import image_reframe, nodes, optional_image

NODE_CLASS_MAPPINGS = {**nodes.NODE_CLASS_MAPPINGS, **image_reframe.NODE_CLASS_MAPPINGS,
                       **optional_image.NODE_CLASS_MAPPINGS}
NODE_DISPLAY_NAME_MAPPINGS = {**nodes.NODE_DISPLAY_NAME_MAPPINGS, **image_reframe.NODE_DISPLAY_NAME_MAPPINGS,
                              **optional_image.NODE_DISPLAY_NAME_MAPPINGS}

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
