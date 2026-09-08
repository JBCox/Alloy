"""Packaging (spec Phase 4): alloy.spec carries the builder package and its in-Blender scripts as data and no longer
excludes Pillow; the launch scripts start the CLI and the builder window. PyInstaller is not installed here, so the
spec is checked as text and a build is not attempted (layer D)."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _spec() -> str:
    return (ROOT / "alloy.spec").read_text(encoding="utf-8")


def test_spec_no_longer_excludes_pillow():
    text = _spec()
    excludes = re.search(r"excludes=\[(.*?)\]", text, re.S).group(1)
    assert "'PIL'" not in excludes and '"PIL"' not in excludes
    assert "'pytest'" in excludes                       # tests stay out of the bundle


def test_spec_bundles_the_builder_package_and_the_blender_scripts_as_data():
    text = _spec()
    hidden = re.search(r"hiddenimports=\[(.*?)\]", text, re.S).group(1)
    for mod in ("'builder'", "'builder.cli'", "'gui.builder_view'", "'PIL.Image'", "'PIL.ImageTk'", "'sqlite3'"):
        assert mod in hidden, mod
    datas = re.search(r"datas=\[(.*?)\]", text, re.S).group(1)
    assert "builder" in datas and "scripts" in datas    # the scripts run INSIDE Blender from files, never from the PYZ
    assert (ROOT / "requirements.txt").read_text(encoding="utf-8").count("Pillow") == 1


@pytest.mark.parametrize("script,needle", [("builder.bat", "harness"), ("model-builder.bat", "builder_view")])
def test_launch_scripts_exist_and_name_their_entry_points(script, needle):
    path = ROOT / "scripts" / script
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "\r\n" not in text                            # LF like every tracked file
    if script == "builder.bat":
        assert "python" in text and "-m builder" in text
        out = subprocess.run([str(path), "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             cwd=str(ROOT), timeout=60)
        assert out.returncode == 0 and needle in out.stdout
    else:
        assert "-m gui.builder_view" in text
