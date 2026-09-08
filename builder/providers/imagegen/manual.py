"""The manual image seat (addendum D12): the owner operates the ChatGPT or Gemini app.

Alloy's art director writes the prompt and names the images to attach; this seat writes them to
``concept/requests/<id>/PROMPT.md`` (plus copies of the attachments in ``attach/``); the owner generates, saves the
files, and runs ``concept import``. Vendor and model are recorded as declared by the owner; Alloy cannot verify them
and says so. No credentials, no automated call, no vendor cost accounting.
"""
from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import Any

from ...ids import sha256_file, utc_now
from .base import ImageSeat

PROVENANCE = "declared by the owner at import; Alloy cannot verify the vendor or model and records both as declarations"
LIVE_NOT_APPLICABLE = ("not applicable for the manual seat: provenance is as declared by the owner; consistency of every "
                       "generated image is checked independently by both LLM seats (R-102, R-109)")


class ManualImageSeat(ImageSeat):
    name = "manual"
    kind = "manual"

    def __init__(self, vendor: str = "chatgpt", model: str = ""):
        self.vendor = vendor or "chatgpt"
        self.model = model or ""

    def declared(self) -> dict[str, Any]:
        return {"seat": "manual", "label": self.label, "vendor": self.vendor,
                "model": self.model or "as declared by the owner at import (per request)",
                "mode": "the owner generates in the vendor's app from the prompt Alloy wrote and imports the files",
                "provenance": PROVENANCE, "cost": "not applicable (no vendor cost accounting for a manual seat, R-105)"}

    def publish_request(self, request_dir: Path, *, request_id: str, target: dict[str, Any], prompt: str,
                        attachments: list[dict[str, Any]], expected_count: int, import_command: str) -> dict[str, Any]:
        rdir = Path(request_dir)
        attach_dir = rdir / "attach"
        attach_dir.mkdir(parents=True, exist_ok=True)
        copies: list[dict[str, Any]] = []
        for n, att in enumerate(attachments, 1):
            src = Path(att["path"])
            dest = attach_dir / f"{n:02d}_{att.get('role', 'image')}_{att.get('reference_id', src.stem)}{src.suffix.lower()}"
            shutil.copyfile(src, dest)
            digest = sha256_file(dest)
            if att.get("sha256") and digest != att["sha256"]:
                dest.unlink(missing_ok=True)
                raise OSError(f"attachment copy hash differs from the reference {att.get('reference_id')} (R-48)")
            copies.append({**att, "copy": str(dest), "sha256": digest})
        lines = [f"# Generation request {request_id}", "",
                 f"- target: {_target_text(target)}",
                 f"- seat: manual ({self.vendor}); vendor and model are recorded as declarations when you import",
                 f"- images expected: {expected_count}",
                 f"- written: {utc_now()}", "",
                 "## Prompt (paste as-is)", "", "```text", prompt.rstrip("\n"), "```", ""]
        if copies:
            lines += ["## Attach these images, in this order", ""]
            for c in copies:
                lines.append(f"- {c['copy']}  ({c.get('role', 'image')}, reference {c.get('reference_id')}, sha256 {c['sha256'][:12]}...)")
            lines.append("")
        else:
            lines += ["## Attachments", "", "- none (this request has no conditioning image)", ""]
        lines += ["## Then import", "",
                  "Save the generated image(s) unmodified (PNG preferred) and run:", "", "```text", import_command, "```", "",
                  "Add `--vendor chatgpt|gemini|other` and `--model \"<as shown in the app>\"` to declare what generated them. "
                  "Alloy records these as declarations, never as verified facts (R-96a). A generated image is a hypothesis "
                  "until it is approved (R-32, A.1).", ""]
        prompt_file = rdir / "PROMPT.md"
        with open(prompt_file, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))
        return {"prompt_file": str(prompt_file), "attachments": copies, "expected_count": expected_count,
                "attach_dir": str(attach_dir)}

    def preflight(self, *, import_dir: Path, flow_confirmed: bool) -> dict[str, Any]:
        declared = self.declared()
        local = {"import_dir": str(import_dir), "import_dir_writable": False, "flow_confirmed": bool(flow_confirmed),
                 "evidence": ""}
        writable, why = _writable(Path(import_dir))
        local["import_dir_writable"] = writable
        if not writable:
            local_tier = "missing"
            local["evidence"] = f"import directory is not writable: {why}"
        elif not flow_confirmed:
            local_tier = "not_confirmed"
            local["evidence"] = ("import directory writable; the generate-and-import flow has not been confirmed once "
                                 "(run `concept prompts`, generate, then `concept import` for one request)")
        else:
            local_tier = "ok"
            local["evidence"] = "import directory writable; the generate-and-import flow was confirmed once"
        live = {"evidence": LIVE_NOT_APPLICABLE}
        blockers = []
        if local_tier == "missing":
            blockers.append(f"image seat: {local['evidence']}. Remediation: set builder.concept.import_dir to a writable folder.")
        return {"seat": self.label, "declared": declared, "local": local, "live": live,
                "tiers": {"declared": "manual", "local": local_tier, "live": "not_applicable"},
                "ok": not blockers, "blockers": blockers, "at": utc_now()}


def _target_text(target: dict[str, Any]) -> str:
    kind = target.get("kind")
    if kind == "anchor_candidate":
        return f"anchor candidates ({target.get('count', 1)} images from the same prompt)"
    if kind == "view":
        return f"turnaround view '{target.get('view')}' conditioned on the anchor"
    if kind == "study":
        return f"study of part {target.get('part_id')} ({target.get('view')}{', ' + str(target.get('region')) if target.get('region') else ''})"
    return str(target)


def _writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"{path}: {exc}"
    if not path.is_dir():
        return False, f"{path} is not a directory"
    probe = path / f".alloy-write-probe-{secrets.token_hex(4)}"
    try:
        probe.write_bytes(b"probe")
        probe.unlink()
    except OSError as exc:
        return False, f"{path}: {exc}"
    return True, ""
