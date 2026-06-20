"""
core — ASCILINE engine internals.

Public surface:
    VideoDecoder   : frame producer (opencv wrapper)
    AsciiMapper    : gray+BGR → ASCII/colour string
    encode_frame   : adaptive per-frame codec (re-exported from codec.py)
"""
from .decoder import VideoDecoder, AsciiMapper
from codec import encode_frame

__all__ = ["VideoDecoder", "AsciiMapper", "encode_frame"]
