"""Image-generation seats (addendum D10, D12). The manual seat is the default and the only one in this build."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .base import ImageSeat


def make_image_seat(settings: dict[str, str]) -> "ImageSeat":
    """Build the seat ``builder.image_generation`` names. Unknown seats are refused, never substituted (R-20)."""
    seat = (settings.get("seat") or "manual").strip().lower()
    if seat == "manual":
        from .manual import ManualImageSeat

        return ManualImageSeat(vendor=settings.get("vendor") or "chatgpt", model=settings.get("model") or "")
    raise ValueError(f"image seat {seat!r} is not available in this build; only 'manual' exists (an API seat would take "
                     "its credentials from the environment only, D12)")
