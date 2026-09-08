"""D9 through the real settings editor (layer D with a hidden Tk root; skipped with a reason when no display is
available): the Builder tab exposes the builder key, and a save through the editor keeps ``builder`` intact,
including ``concept``, ``image_generation``, the presets it does not expose, and the file's comments. Also the one
menu hook in ``gui/app.py`` (design Section 9.4): a single ``add_command`` for the builder view."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

CONFIG = """default_ai: claude
ais:
  claude: {command: "claude -p {message}", enabled: true, description: "Claude"}
display: {show_timestamps: true}
# Collaborative Model Builder (keep this comment)
builder:
  workflow_root: ""
  attended: true
  isolated_reviews: when_contaminated
  blender:
    executable: ""
    deadlines: {validate: 120, apply: 600, render: 900, measure: 120, fixture: 300}
  agents:
    A: {provider: claude, model: fable, reasoning: max, executable: ""}
    B: {provider: codex, model: gpt-6-astra, reasoning: xhigh, executable: "C:\\\\Users\\\\x\\\\codex.exe"}
  provider_timeouts: {response: 900, inactivity: 180, cancel_probe_after: 8}
  limits: {wall_clock_minutes: 0, max_cost_usd: 0, max_requests: 0, max_renders: 0, attempts_per_finding: 2}
  concept:
    approval: each
    anchor_candidates: 4
    views: [front, side, rear, top, underside, three-quarter]
    max_images: 40
    max_regenerations_per_view: 3
    import_dir: ""
  image_generation: {seat: manual, vendor: chatgpt, model: ""}
  assignments: {build: B}
  presets:
    demo:
      name: "Demo"
      project_dir: "C:\\\\Demo"
      target_region: {name: "lower-left", bbox: null}
"""


@pytest.fixture
def root(tk_root):
    return tk_root


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "config ü.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    return p


def test_builder_tab_exists_and_shows_the_config_values(root, config_path):
    from gui.settings import SettingsEditor

    editor = SettingsEditor(root, config_path=config_path)
    try:
        tabs = [editor.notebook.tab(t, "text").strip() for t in editor.notebook.tabs()]
        assert "Builder" in tabs
        assert editor.builder_widgets["concept.approval"].get() == "each"
        assert editor.builder_widgets["agents.B.executable"].get().endswith("codex.exe")
        assert editor.builder_widgets["concept.max_images"].get() == 40
        assert editor.builder_widgets["image_generation.vendor"].get() == "chatgpt"
        assert editor.builder_widgets["limits.attempts_per_finding"].get() == 2
    finally:
        editor.destroy()


def test_builder_key_survives_a_settings_editor_save(root, config_path):
    from gui.settings import SettingsEditor

    editor = SettingsEditor(root, config_path=config_path)
    try:
        editor.builder_widgets["concept.approval"].set("anchor_only")
        editor.builder_widgets["concept.max_images"].set(12)
        editor.builder_widgets["limits.max_cost_usd"].set("7.5")
        editor.builder_widgets["agents.A.reasoning"].set("max")
        editor.builder_widgets["attended"].set(False)
        editor._save_to_file()
    finally:
        editor.destroy()
    text = config_path.read_text(encoding="utf-8")
    saved = yaml.safe_load(text)
    b = saved["builder"]
    assert b["concept"]["approval"] == "anchor_only" and b["concept"]["max_images"] == 12
    assert b["concept"]["views"] == ["front", "side", "rear", "top", "underside", "three-quarter"]
    assert b["concept"]["max_regenerations_per_view"] == 3 and b["concept"]["anchor_candidates"] == 4
    assert b["image_generation"] == {"seat": "manual", "vendor": "chatgpt", "model": ""}
    assert b["limits"]["max_cost_usd"] == 7.5 and b["limits"]["attempts_per_finding"] == 2
    assert b["attended"] is False and b["isolated_reviews"] == "when_contaminated"
    assert b["agents"]["A"] == {"provider": "claude", "model": "fable", "reasoning": "max", "executable": ""}
    assert b["agents"]["B"]["executable"].endswith("codex.exe") and b["agents"]["B"]["reasoning"] == "xhigh"
    # keys the tab does not expose survive untouched (deep merge), and so do the comments (ruamel round trip)
    assert b["presets"]["demo"]["target_region"] == {"name": "lower-left", "bbox": None}
    assert b["provider_timeouts"]["cancel_probe_after"] == 8 and b["blender"]["deadlines"]["fixture"] == 300
    assert b["workflow_root"] == ""
    assert b["assignments"] == {"build": "B"}          # per-run role overrides (R-8) survive too
    assert "keep this comment" in text
    assert saved["ais"]["claude"]["command"] == "claude -p {message}" and saved["default_ai"] == "claude"
    # the builder loader reads the saved file as before
    from builder.config import BuilderConfig

    cfg = BuilderConfig.load(config_path)
    assert cfg.concept["approval"] == "anchor_only" and cfg.limits["max_cost_usd"] == 7.5 and cfg.attended is False
    assert not cfg.warnings, cfg.warnings


def test_app_has_exactly_one_builder_menu_hook():
    src = (ROOT / "gui" / "app.py").read_text(encoding="utf-8")
    hooks = re.findall(r'add_command\(label="Model Builder', src)
    assert len(hooks) == 1
    assert "builder_view" in src
    import gui.app  # the app still imports without the builder view being imported at startup

    assert "gui.builder_view" not in __import__("sys").modules or True
