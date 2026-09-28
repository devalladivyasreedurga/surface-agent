"""Input target specification -- what discover.py / replay.py accept.

Kept intentionally minimal: {"type": "web", "url": "..."}. The `type` field is
forward-looking (a DesktopSurface would use type="desktop" with different
fields) but only "web" is implemented in this assignment.
"""
from __future__ import annotations

from pydantic import BaseModel


class TargetSpec(BaseModel):
    type: str = "web"
    url: str
