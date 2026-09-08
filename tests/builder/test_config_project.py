"""D9 builder config key, R-93 presets never touch sources, project layout, default workflow dir."""
from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest
import yaml

from builder.config import BuilderConfig, default_workflow_dir, discover_blender, slugify
from builder.project import LAYOUT, Project


def test_builder_config_defaults_when_key_missing():
    cfg = BuilderConfig.from_dict({})
    assert cfg.attended is True
    assert cfg.isolated_reviews == "when_contaminated"
    assert cfg.agents["A"].provider == "claude" and cfg.agents["A"].model == "fable" and cfg.agents["A"].reasoning == "max"
    assert cfg.agents["B"].provider == "codex" and cfg.agents["B"].model == "gpt-6-astra" and cfg.agents["B"].reasoning == "xhigh"
    assert cfg.deadlines["render"] == 900 and cfg.deadlines["validate"] == 120
    assert cfg.provider_timeouts["response"] == 900 and cfg.provider_timeouts["inactivity"] == 180
    assert cfg.limits["attempts_per_finding"] == 2
    assert cfg.workflow_root == "" and cfg.blender_executable == ""
    assert cfg.presets == {}
    assert cfg.warnings == []


def test_builder_config_is_lenient_and_keeps_unknown_keys():
    cfg = BuilderConfig.from_dict({
        "builder": {
            "attended": False, "isolated_reviews": "always", "workflow_root": "D:/wf",
            "blender": {"executable": "X:/b.exe", "deadlines": {"render": 5, "bogus": 1}},
            "agents": {"A": {"provider": "gemini"}, "Z": {"provider": "codex"}},
            "limits": {"max_renders": "12", "attempts_per_finding": "nope"},
            "future_key": {"x": 1},
        }
    })
    assert cfg.attended is False and cfg.isolated_reviews == "always" and cfg.workflow_root == "D:/wf"
    assert cfg.blender_executable == "X:/b.exe" and cfg.deadlines["render"] == 5 and cfg.deadlines["apply"] == 600
    assert cfg.agents["A"].provider == "gemini" and cfg.agents["A"].model == ""  # override replaces defaults for that agent
    assert cfg.agents["B"].provider == "codex"
    assert "Z" not in cfg.agents
    assert cfg.limits["max_renders"] == 12
    assert cfg.limits["attempts_per_finding"] == 2 and any("attempts_per_finding" in w for w in cfg.warnings)
    assert cfg.raw["future_key"] == {"x": 1}


def test_builder_config_rejects_bad_isolated_reviews_value_to_default():
    cfg = BuilderConfig.from_dict({"builder": {"isolated_reviews": "never"}})
    assert cfg.isolated_reviews == "when_contaminated"
    assert any("isolated_reviews" in w for w in cfg.warnings)


def test_discover_blender_prefers_configured_then_highest_version(tmp_path):
    root = tmp_path / "Blender Foundation"
    for ver in ("Blender 4.2", "Blender 5.2", "Blender 5.10"):
        (root / ver).mkdir(parents=True)
        (root / ver / "blender.exe").write_bytes(b"")
    assert discover_blender("", search_roots=[root]) == root / "Blender 5.10" / "blender.exe"
    configured = tmp_path / "custom dir" / "blender.exe"
    configured.parent.mkdir()
    configured.write_bytes(b"")
    assert discover_blender(str(configured), search_roots=[root]) == configured
    assert discover_blender(str(tmp_path / "nope.exe"), search_roots=[tmp_path / "empty"]) is None


def test_builder_config_loads_from_config_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("default_ai: claude\nais: {}\nbuilder:\n  attended: false\n  presets:\n    demo: {name: Demo}\n", encoding="utf-8")
    cfg = BuilderConfig.load(p)
    assert cfg.attended is False and cfg.presets["demo"]["name"] == "Demo"
    missing = BuilderConfig.load(tmp_path / "absent.yaml")
    assert missing.attended is True


def test_builder_config_loads_json_by_extension(tmp_path):
    p = tmp_path / "builder.json"
    p.write_text(json.dumps({"version": 1, "recent": [{"workflow_dir": "X:/wf"}],
                             "builder": {"attended": False, "presets": {"demo": {"name": "Demo"}}}}),
                 encoding="utf-8")
    cfg = BuilderConfig.load(p)
    assert cfg.attended is False and cfg.presets["demo"]["name"] == "Demo"
    assert cfg.warnings == []            # `version` and `recent` live outside the builder key and are invisible
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert BuilderConfig.load(empty).attended is True


def test_builder_config_env_var_and_missing_mean_defaults(tmp_path, monkeypatch):
    from builder.config import ENV_SETTINGS_PATH

    monkeypatch.delenv(ENV_SETTINGS_PATH, raising=False)
    monkeypatch.chdir(tmp_path)                       # no ./sessions/builder.json here either
    assert BuilderConfig.load().attended is True     # defaults, and no host module was imported
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"builder": {"attended": False}}), encoding="utf-8")
    monkeypatch.setenv(ENV_SETTINGS_PATH, str(p))
    assert BuilderConfig.load().attended is False
    monkeypatch.setenv(ENV_SETTINGS_PATH, str(tmp_path / "gone.json"))
    assert BuilderConfig.load().attended is True
    monkeypatch.delenv(ENV_SETTINGS_PATH)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "builder.json").write_text(json.dumps({"builder": {"attended": False}}), encoding="utf-8")
    assert BuilderConfig.load().attended is False    # the app's layout, when run from its root


def test_settings_file_rejects_unknown_extension_and_non_mapping(tmp_path):
    from builder.config import read_settings_file

    toml = tmp_path / "builder.toml"
    toml.write_text("attended = false", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        read_settings_file(toml)
    assert "builder.toml" in str(exc.value)
    lst = tmp_path / "list.json"
    lst.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError) as exc2:
        read_settings_file(lst)
    assert "list.json" in str(exc2.value)


def test_slugify_and_default_workflow_dir(tmp_path):
    assert slugify("E10 Bearer - Head") == "e10-bearer-head"
    assert slugify("  Ünïcode 名 name!! ") == "unicode-name"
    cfg = BuilderConfig.from_dict({})
    assert default_workflow_dir(tmp_path / "proj", "e10-bearer-head", cfg) == tmp_path / "proj" / "alloy-builder" / "e10-bearer-head"
    cfg2 = BuilderConfig.from_dict({"builder": {"workflow_root": str(tmp_path / "root")}})
    assert default_workflow_dir(tmp_path / "proj", "slug", cfg2) == tmp_path / "root" / "slug"
    assert default_workflow_dir(None, "slug", cfg) == Path.cwd() / "alloy-builder" / "slug"


def test_project_create_layout_and_reopen(workdir):
    wf = workdir / "wf"
    prj = Project.create(wf, name="Demo asset", asset_name="Demo")
    try:
        for sub in LAYOUT:
            assert (wf / sub).is_dir(), sub
        assert (wf / "project.json").is_file() and (wf / "builder.sqlite3").is_file()
        assert prj.record.data["name"] == "Demo asset"
        assert prj.store.journal()[0].event == "project.created"
        pid = prj.record.id
    finally:
        prj.close()
    with pytest.raises(FileExistsError):
        Project.create(wf, name="again", asset_name="x")
    with Project.open(wf) as again:
        assert again.record.id == pid
    with pytest.raises(FileNotFoundError):
        Project.open(workdir / "nope")


def test_preset_creation_never_opens_sources(workdir, monkeypatch):
    src_png = str(workdir / "art" / "concept.png")
    src_blend = str(workdir / "art" / "model.blend")
    preset = {
        "name": "E10 Bearer - Head", "project_dir": str(workdir / "proj"), "asset": "E10 Bearer",
        "target_reference": src_png, "target_region": {"name": "lower-left humanoid", "bbox": None},
        "existing_source": src_blend, "existing_source_trust": "unverified", "first_component": "Head",
        "rejected_references": [str(workdir / "art" / "old.png")],
    }
    real_open = builtins.open

    def guarded_open(file, *a, **kw):
        if isinstance(file, (str, Path)) and "art" in str(file).replace("\\", "/").split("/"):
            raise AssertionError(f"preset creation opened a source file: {file}")
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", guarded_open)
    try:
        import PIL.Image as PILImage

        monkeypatch.setattr(PILImage, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("PIL opened a source")))
    except ImportError:
        pass
    prj = Project.create_from_preset("e10-bearer-head", preset, workflow_dir=workdir / "wf")
    try:
        d = prj.record.data
        assert d["preset_id"] == "e10-bearer-head" and d["asset_name"] == "E10 Bearer"
        assert d["first_component"] == "Head"
        assert d["target_reference"]["path"] == src_png and d["target_reference"]["region"]["bbox"] is None
        assert d["existing_source"]["path"] == src_blend and d["existing_source"]["trust"] == "unverified"
        assert d["rejected_references"] == [str(workdir / "art" / "old.png")]
        assert prj.store.list("reference") == []          # intake happens later, explicitly
        assert not (workdir / "proj").exists()             # nothing created under the user's project
        assert d["status_note"].startswith("preset created")
    finally:
        prj.close()


def test_importing_builder_writes_nothing(tmp_path, monkeypatch):
    import importlib

    before = sorted(str(p) for p in Path(__file__).resolve().parents[2].glob("*"))
    monkeypatch.chdir(tmp_path)
    import builder.config as bc
    import builder.project as bp

    importlib.reload(bc)
    importlib.reload(bp)
    after = sorted(str(p) for p in Path(__file__).resolve().parents[2].glob("*"))
    assert before == after
    assert list(tmp_path.iterdir()) == []
