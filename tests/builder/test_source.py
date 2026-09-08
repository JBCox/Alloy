"""Revision 0 for a project created with ``new`` (layer D, fake runner): ``source register <wf> <file.blend>``
validates the owner's file in separate Blender processes (identities, reopen), copies it unmodified into
``revisions/`` as an immutable revision with its identity map and asset dependencies, and journals it;
``source empty <wf>`` builds an empty scene through the runner as revision 0. Creating the project never opens
the source (R-93); registering it is the explicit step. The fake runner proves routing, the copy, the records
and the journal, never Blender behaviour (the real-Blender tests are in ``test_blender_source.py``)."""
from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from builder import cli
from builder.ids import sha256_file
from builder.project import Project

from _fake_runner import FakeRunner
from _screenplay import PARTS, write_screenplay


def run_cli(*argv, runner=None):
    out = io.StringIO()
    code = cli.main([str(a) for a in argv], runner_factory=(lambda cfg, project: runner), stdout=out)
    return code, out.getvalue()


@pytest.fixture
def wf(workdir):
    d = workdir / "wf ü"
    code, out = run_cli("new", "--workflow-dir", d, "--name", "Mast ü", "--asset", "E11 Mast", "--first-component", "Head")
    assert code == 0, out
    return d


@pytest.fixture
def source(workdir):
    p = workdir / "owner source ü.blend"
    p.write_bytes(b"BLENDER-owner-source-bytes")
    return p


def _revisions(wf: Path):
    with Project.open(wf) as prj:
        return [dict(id=r.id, **r.data) for r in prj.store.list("revision")]


def _journal_events(wf: Path):
    with Project.open(wf) as prj:
        return [e.event for e in prj.store.journal()]


def test_new_never_registers_a_revision(wf):
    """R-93: creating the project records nothing about a source; revision 0 is an explicit step."""
    assert _revisions(wf) == []
    assert not any(Path(wf, "revisions").iterdir())


def test_source_register_validates_in_blender_copies_unmodified_and_journals(wf, source):
    runner = FakeRunner()
    runner.identity_map = [{"alloy_id": "p_head", "type": "OBJECT", "alloy_kind": "part", "name": "Head"},
                           {"alloy_id": "mat_base", "type": "MATERIAL", "alloy_kind": "material", "name": "Base"}]
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    code, out = run_cli("source", "register", wf, source, "--note", "owner's v1", runner=runner)
    assert code == 0, out
    # validated in separate Blender processes: identities, then reopen/validate (never apply, never a save)
    assert any(c.startswith("identities:") for c in runner.calls) and any(c.startswith("validate:") for c in runner.calls)
    assert not any(c.startswith("apply:") for c in runner.calls)
    # the source is untouched
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    revs = _revisions(wf)
    assert len(revs) == 1 and revs[0]["id"] in out
    rev = revs[0]
    dest = Path(rev["file"])
    assert dest.parent == wf / "revisions" and dest.is_file()
    assert rev["sha256"] == sha256_file(source) == sha256_file(dest)          # copied byte for byte
    assert not (dest.stat().st_mode & stat.S_IWRITE)                          # immutable (R-40)
    assert rev["parent_revision_id"] is None and rev["immutable"] is True
    assert rev["identity_map"] == runner.identity_map
    assert rev["source"]["path"] == str(source) and rev["source"]["sha256"] == rev["sha256"]
    assert "owner's v1" in rev["note"]
    # the part inventory mirrors the tagged objects (R-37); materials are not parts
    with Project.open(wf) as prj:
        parts = prj.store.list("part")
        assert [p.id for p in parts] == ["p_head"] and parts[0].data["blender_ids"] == ["p_head"]
        ok, expected, actual = prj.store.verify_file(dest)
        assert ok
    events = _journal_events(wf)
    assert "source.validated" in events and "source.registered" in events and "revision.created" in events
    # status shows revision 0
    code, out = run_cli("status", wf, "--json", runner=runner)
    assert code == 0 and json.loads(out)["revision_id"] == rev["id"]
    code, out = run_cli("status", wf, runner=runner)
    assert code == 0 and f"revision: {rev['id']}" in out


def test_source_register_refuses_a_second_revision_0(wf, source):
    runner = FakeRunner()
    assert run_cli("source", "register", wf, source, runner=runner)[0] == 0
    code, out = run_cli("source", "register", wf, source, runner=runner)
    assert code == 2 and "already" in out and "rev_" in out
    assert len(_revisions(wf)) == 1


def test_source_register_refuses_a_file_blender_cannot_validate(wf, source):
    runner = FakeRunner()
    runner.validate_ok = False
    code, out = run_cli("source", "register", wf, source, runner=runner)
    assert code == 2 and "validation" in out and "duplicate alloy_id" in out
    assert _revisions(wf) == [] and not any(Path(wf, "revisions").iterdir())
    events = _journal_events(wf)
    assert "source.rejected" in events and "revision.created" not in events


def test_source_register_refuses_missing_or_non_blend_files(wf, workdir):
    runner = FakeRunner()
    code, out = run_cli("source", "register", wf, workdir / "nope.blend", runner=runner)
    assert code == 2 and "not found" in out and runner.calls == []
    txt = workdir / "notes.txt"
    txt.write_text("x", encoding="utf-8")
    code, out = run_cli("source", "register", wf, txt, runner=runner)
    assert code == 2 and ".blend" in out and runner.calls == []
    assert _revisions(wf) == []


def test_source_register_records_unmapped_geometry_without_modifying_the_file(wf, source):
    """An owner's file usually carries no alloy_id: the untagged geometry is recorded, never tagged in place."""
    runner = FakeRunner()
    runner.identity_map = []
    runner.validate_unmapped = ["Cube", "Cylinder.001"]
    code, out = run_cli("source", "register", wf, source, runner=runner)
    assert code == 0, out
    rev = _revisions(wf)[0]
    assert rev["source"]["unmapped_geometry"] == ["Cube", "Cylinder.001"] and rev["identity_map"] == []
    assert "2 object(s) without alloy_id" in out
    assert sha256_file(source) == rev["sha256"]


def test_source_empty_builds_revision_0_through_the_runner(wf):
    runner = FakeRunner()
    runner.identity_map = [{"alloy_id": "col_model", "type": "COLLECTION", "alloy_kind": "collection", "name": "Model"}]
    code, out = run_cli("source", "empty", wf, runner=runner)
    assert code == 0, out
    assert any(c.startswith("build_scene:") for c in runner.calls) and any(c.startswith("validate:") for c in runner.calls)
    revs = _revisions(wf)
    assert len(revs) == 1 and revs[0]["id"] in out
    rev = revs[0]
    assert Path(rev["file"]).is_file() and rev["source"]["kind"] == "empty_scene"
    assert "empty scene" in rev["note"]
    assert not (Path(rev["file"]).stat().st_mode & stat.S_IWRITE)
    # nothing left in staging; parts inventory stays empty (collections are not parts)
    assert not any(p for p in Path(wf, "staging").rglob("*") if p.is_file())
    with Project.open(wf) as prj:
        assert prj.store.list("part") == []
    assert "source.registered" in _journal_events(wf)
    code, out = run_cli("source", "empty", wf, runner=runner)
    assert code == 2 and "already" in out


def test_source_empty_then_start_reaches_the_build_stage(wf, workdir):
    """The gap named in known-limitations: a `new` project used to pause with execution_failure at build."""
    from PIL import Image

    runner = FakeRunner()
    runner.identity_map = [{"alloy_id": p, "type": "OBJECT"} for p in PARTS]
    ref = workdir / "front.png"
    Image.new("RGB", (100, 80), (70, 70, 70)).save(ref)
    assert run_cli("intake", wf, "--ref", ref, "--labels", "front", runner=runner)[0] == 0
    assert run_cli("source", "empty", wf, runner=runner)[0] == 0
    play = write_screenplay(workdir / "play.json")
    assert run_cli("preflight", wf, "--live", "--mock", play, runner=runner)[0] == 0
    code, out = run_cli("start", wf, "--mock", play, "--max-steps", "60", runner=runner)
    # the scripted seats name the fixture's part ids while this project's parts come from the plan, so the gate's
    # coverage check refuses at the end; what matters here is that the run got past the plan into build and review
    assert "no revision exists" not in out, out
    assert "[stage] build" in out and "[stage] review" in out
    with Project.open(wf) as prj:
        kinds = [t.data["kind"] for t in prj.store.list("task")]
        assert "build" in kinds and "review" in kinds
        revs = prj.store.list("revision")
        assert len(revs) >= 2 and revs[0].data["source"]["kind"] == "empty_scene" and revs[1].data["parent_revision_id"] == revs[0].id


def test_help_lists_the_source_verb():
    out = io.StringIO()
    cli.main(["--help"], stdout=out)
    assert "source" in out.getvalue()
    out = io.StringIO()
    cli.main(["source", "--help"], stdout=out)
    text = out.getvalue()
    assert "register" in text and "empty" in text
