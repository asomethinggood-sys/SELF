"""Opt-in ComfyUI extension for experimental distributed SDXL."""
from .coordinator import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .worker import register_routes

register_routes()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
