"""Image seat contract (addendum D10, D12, R-96, R-105, R-109).

The image seat ``I`` is a tool: it generates from prompts the art director wrote and judges nothing. The concept
loop (``builder.concept``) never depends on how a seat generates: it publishes a request, and images come back
through ``concept import`` (manual seat) or ``generate`` (an API seat, if one is ever added). Provenance of manual
imports is declared by the owner and recorded as a declaration.

Credentials policy for any future API seat: credentials come from the environment only and never appear in argv,
records, logs, or packets. This module and the manual seat handle no credentials at all.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from ...records import NOT_APPLICABLE, Quantity

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


class ImageSeat(ABC):
    name: str = "image-seat"
    label: str = "I"
    kind: str = "manual"          # manual | api
    SECRETS_POLICY = "environment_only_never_in_argv_records_logs_or_packets"

    @abstractmethod
    def declared(self) -> dict[str, Any]:
        """Declared tier (R-109): seat kind, vendor, model, and how provenance is established."""

    @abstractmethod
    def publish_request(self, request_dir: Path, *, request_id: str, target: dict[str, Any], prompt: str,
                        attachments: list[dict[str, Any]], expected_count: int, import_command: str) -> dict[str, Any]:
        """Write everything the generating side needs (for the manual seat: PROMPT.md and the attachment copies).
        Returns ``{"prompt_file", "attachments": [{... , "copy"}], "expected_count"}``."""

    def generate(self, request: dict[str, Any]) -> list[Path] | None:
        """Produce the images for ``request``. ``None`` means the seat does not generate by itself (manual: the
        owner generates in an app and imports the files)."""
        return None

    @abstractmethod
    def preflight(self, *, import_dir: Path, flow_confirmed: bool) -> dict[str, Any]:
        """Three tiers (R-109) as ``{"declared", "local", "live", "tiers": {...}, "ok", "blockers"}``."""

    def cost_quantity(self) -> Quantity:
        """Cost of one generation as the seat can account for it (R-105). Manual: not applicable, never zero."""
        return NOT_APPLICABLE


def watch_folder(folder: str | Path, on_file: Callable[[Path], None], *, poll_s: float = 1.0,
                 stop: threading.Event | None = None, timeout_s: float | None = None, ignore_existing: bool = True,
                 suffixes: tuple[str, ...] = IMAGE_SUFFIXES) -> dict[str, Any]:
    """Call ``on_file`` once for every new image that appears in ``folder`` (R-110 ``concept import --watch``).
    A file is handed over only after its size has been stable for one poll, so a file still being written is
    not imported half-way. Stops on ``stop``, on ``timeout_s``, or when ``on_file`` raises ``StopIteration``."""
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    stop = stop or threading.Event()
    seen: set[str] = set()
    sizes: dict[str, int] = {}
    if ignore_existing:
        seen.update(str(p) for p in root.iterdir() if p.is_file())
    started = time.monotonic()
    imported = 0
    stopped_by = "stop"
    errors: list[dict[str, str]] = []
    while True:
        if stop.is_set():
            stopped_by = "stop"
            break
        if timeout_s is not None and time.monotonic() - started >= timeout_s:
            stopped_by = "timeout"
            break
        for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_file() or str(p) in seen or p.suffix.lower() not in suffixes:
                continue
            size = p.stat().st_size
            if sizes.get(str(p)) != size:
                sizes[str(p)] = size     # wait one more poll for the size to settle
                continue
            seen.add(str(p))
            try:
                on_file(p)
                imported += 1
            except StopIteration:
                stopped_by = "handler"
                stop.set()
                break
            except Exception as exc:  # noqa: BLE001 - one bad file never stops the watch; it is reported
                errors.append({"file": str(p), "error": str(exc)})
        if stop.is_set():
            break
        time.sleep(poll_s)
    return {"imported": imported, "stopped_by": stopped_by, "errors": errors, "folder": str(root),
            "elapsed_s": round(time.monotonic() - started, 3)}
