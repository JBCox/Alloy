"""A JSON-serialisable mock screenplay shared by the CLI tests and the real-Blender end-to-end test.

The screenplay itself lives in ``builder.harness.fixture_screenplay`` (the efficiency harness runs the same one),
so the tests and the harness can never drift apart. Scripts are real bpy so the same screenplay drives the fake
runner (which ignores them) and real Blender. Placeholders such as "{finding_id}" and "{finding_ids}" are filled
by the mock from the request metadata Alloy attaches, so the screenplay never guesses ids."""
from __future__ import annotations

import json
from pathlib import Path

from builder.harness import FIXTURE_PARTS as PARTS  # noqa: F401  (re-export for the CLI tests)
from builder.harness import fixture_screenplay


def screenplay(**kw):
    return fixture_screenplay(**kw)


def write_screenplay(path: Path, **kw) -> Path:
    path.write_text(json.dumps(screenplay(**kw), ensure_ascii=False, indent=1), encoding="utf-8")
    return path
