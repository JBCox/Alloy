"""A fake Blender runner for deterministic tests. It states exactly what it scripts: file copies with
markers instead of Blender edits, PNGs painted by Pillow instead of renders, canned measurements.
It proves the host-side lifecycle (staging, validation gates, promotion, manifests), never Blender behaviour."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from builder.blender.runner import BlenderResult


class FakeRunner:
    def __init__(self):
        self.calls: list[str] = []
        self.apply_outcome = "ok"
        self.validate_ok = True
        self.render_outcome = "ok"
        self.identity_map = [{"alloy_id": "p_base", "type": "OBJECT"}, {"alloy_id": "p_cap", "type": "OBJECT"}]
        self.measure_data = {"bboxes": {}, "distances": [], "ratios": {}, "overlaps": [], "limitations": ["fake"]}
        self.render_count = 0

    def _result(self, op_id, outcome, data=None, error="", exit_code=0):
        result = None
        if outcome not in ("timeout", "uncertain", "cancelled", "spawn_failed"):
            result = {"ok": outcome == "ok", "op_id": op_id, "stage": "fake", "data": data or {},
                      "errors": [error] if error else [], "warnings": []}
        return BlenderResult(op_id=op_id, outcome=outcome, exit_code=exit_code, result=result, error=error,
                             stdout_path="", stderr_path="", elapsed_s=0.01, kill_confirmed=None, work_dir="")

    def smoke(self):
        self.calls.append("smoke")
        return {"blender_version": "fake-1.0", "python_version": "3.13.0", "engines_ok": ["BLENDER_WORKBENCH", "BLENDER_EEVEE"],
                "engines_listed": ["BLENDER_WORKBENCH", "BLENDER_EEVEE"], "executable": "fake"}

    def apply(self, base_blend, out_blend, script_path, *, op_id, declared_effects, deadline_s=None, work_dir=None,
              cancel_event=None):
        self.calls.append(f"apply:{op_id}")
        out = Path(out_blend)
        if self.apply_outcome == "ok":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(Path(base_blend).read_bytes() + b"|edit:" + op_id.encode())
            return self._result(op_id, "ok", {"identity_map": self.identity_map, "orphans_before_save": [],
                                             "external_assets": [], "base_identity_map": self.identity_map})
        if self.apply_outcome == "timeout":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"partial")
            return self._result(op_id, "timeout", error="timeout after 1.0s", exit_code=None)
        if self.apply_outcome == "uncertain":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(Path(base_blend).read_bytes() + b"|maybe")
            return self._result(op_id, "uncertain", error="exit 0 but no result.json", exit_code=0)
        return self._result(op_id, "failed", error="agent script raised: ValueError", exit_code=3)

    def validate(self, blend, expectations, *, op_id, work_dir=None):
        self.calls.append(f"validate:{op_id}")
        data = {"identity_map": self.identity_map, "deleted": [], "duplicates": [], "unmapped": [], "orphans": [],
                "missing_assets": [], "external_assets": [], "reopened": True}
        if self.validate_ok:
            return self._result(op_id, "ok", data)
        data["duplicates"] = ["p_cap"]
        return self._result(op_id, "failed", data, error="duplicate alloy_id: p_cap")

    def identities(self, blend, *, op_id, work_dir=None):
        self.calls.append(f"identities:{op_id}")
        return self._result(op_id, "ok", {"identity_map": self.identity_map})

    def render(self, blend, view, out_png, *, op_id, deadline_s=None, work_dir=None, cancel_event=None):
        self.calls.append(f"render:{op_id}")
        if self.render_outcome != "ok":
            return self._result(op_id, "failed", error="scripted render failure", exit_code=3)
        self.render_count += 1
        out = Path(out_png)
        out.parent.mkdir(parents=True, exist_ok=True)
        # The image depends on the revision content so different revisions produce different evidence.
        seed = sum(Path(blend).read_bytes()) % 200
        img = Image.new("RGB", tuple(view.get("resolution", [32, 32])), (seed, 80, 120))
        img.save(out)
        applied = {"engine": view.get("engine"), "blender_version": "fake-1.0", "resolution": list(view.get("resolution", [32, 32])),
                   "view_transform": (view.get("color_management") or {}).get("view_transform", "Standard"),
                   "look": "None", "display_device": "sRGB", "samples": view.get("samples"), "seed": None,
                   "hidden_ids": (view.get("visibility") or {}).get("hide") or [], "isolated_ids": (view.get("visibility") or {}).get("isolate"),
                   "lighting": view.get("lighting"), "mode": view.get("mode"), "camera": view.get("camera"), "output": str(out)}
        return self._result(op_id, "ok", {"applied": applied})

    def measure(self, blend, requests, *, op_id, work_dir=None):
        self.calls.append(f"measure:{op_id}")
        return self._result(op_id, "ok", dict(self.measure_data))

    def build_scene(self, out_blend, spec, *, op_id, deadline_s=None, work_dir=None):
        self.calls.append(f"build_scene:{op_id}")
        out = Path(out_blend)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"BLENDER-fake:" + op_id.encode())
        return self._result(op_id, "ok", {"identity_map": self.identity_map, "saved": str(out)})
