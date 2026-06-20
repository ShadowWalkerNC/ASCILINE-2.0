"""
api/models.py
=============
Pydantic request/response models for all Media API endpoints.
"""

from pydantic import BaseModel


class EnqueueBody(BaseModel):
    url:   str
    mode:  int  = 1
    cols:  int | None = None
    vol:   int  = 1
    pixel: bool = False
    loop:  bool = False


class SeekBody(BaseModel):
    time: float


class VolumeBody(BaseModel):
    vol: int


class LoopBody(BaseModel):
    enabled: bool
