"""R-84 efficiency comparison harness (layer D, fake runner, scripted seats): the same fixture run is measured under
packet delivery and under a simulated shared-transcript replay. The tests assert what the harness measures (bytes
recomputed from the records, tokens only where a provider reported them, evidence and coverage counted from the
store) and that the report invents no percentage and claims no quality equivalence."""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from builder import cli, harness
from builder.engine import Engine
from builder.fixture import HEAD_PARTS, create_fixture
from builder.providers.mock import ScriptedAdapter
from builder.records import quantity_from_json

from _fake_runner import FakeRunner
from test_engine import _config

FIXTURE_PARTS = ["p_base", "p_post", "p_housing", "p_bracket", "p_lens", "p_bolt", "p_bolt.1", "p_bolt.2", "p_bolt.3"]


def _fixture_runner() -> FakeRunner:
    r = FakeRunner()
    r.identity_map = [{"alloy_id": p, "type": "OBJECT", "alloy_kind": "instance" if "." in p else "part", "name": p}
                      for p in FIXTURE_PARTS]
    r.measure_data = {"bboxes": {p: {"min": [-0.2, -0.2, i * 0.5], "max": [0.2, 0.2, i * 0.5 + 0.4]} for i, p in enumerate(FIXTURE_PARTS)},
                      "distances": [], "ratios": {}, "overlaps": [], "limitations": ["fake"]}
    return r


@pytest.fixture
def measured(workdir):
    runner = _fixture_runner()
    project, info = create_fixture(workdir / "fixture", runner)
    play = harness.fixture_screenplay()
    adapters = {label: ScriptedAdapter(label, play[label]) for label in ("A", "B")}
    eng = Engine(project, _config(), adapters=adapters, runner=runner)
    eng.preflight(live=True)
    eng.start()
    status = eng.run_until_stop(max_steps=60)
    assert status["stop_reason"] == "ready_for_user_review", status["notes"]
    comparison = harness.compare(project)
    yield project, comparison
    project.close()


def test_packet_delivery_bytes_are_recomputed_from_the_records(measured):
    project, cmp = measured
    store = project.store
    invocations = [i for i in store.list("invocation") if i.data.get("packet_id") and i.data.get("purpose") not in harness.PROBE_PURPOSES]
    expected_bytes = 0
    expected_images = 0
    for inv in invocations:
        packet = store.require("packet", inv.data["packet_id"])
        for f in packet.data["files"]:
            expected_bytes += int(f["bytes"])
            if f["role"] in ("reference", "render", "crop"):
                expected_images += 1
        expected_bytes += harness.prompt_bytes(inv)
    pd = cmp["packet_delivery"]
    assert pd["invocations"] == len(invocations) == cmp["transcript_replay"]["invocations"]
    assert pd["bytes_total"] == expected_bytes
    assert pd["images_delivered"] == expected_images
    assert pd["bytes_text"] + pd["bytes_images"] + pd["bytes_other"] == pd["bytes_total"]
    assert all(t["invocation_id"] and t["purpose"] and t["bytes"] > 0 for t in pd["turns"])
    assert pd["probes"]["invocations"] == len([i for i in store.list("invocation") if i.data.get("purpose") in harness.PROBE_PURPOSES and i.data.get("packet_id")])
    assert not any(t["purpose"] in harness.PROBE_PURPOSES for t in pd["turns"])


def test_transcript_replay_is_simulated_from_the_same_turns_and_never_sent(measured):
    project, cmp = measured
    tr = cmp["transcript_replay"]
    assert tr["simulated"] is True and "nothing was sent" in tr["method"].lower()
    turns = tr["turns"]
    # each replayed turn carries every earlier packet text and reply plus every earlier image again, plus its own packet:
    # the bytes are recomputed here from the packet-delivery turns
    pd_turns = cmp["packet_delivery"]["turns"]
    running_text = 0
    running_images = 0
    for n, (p, t) in enumerate(zip(pd_turns, turns)):
        assert t["invocation_id"] == p["invocation_id"]
        expected = p["bytes"] + running_text + running_images
        assert t["bytes"] == expected, (n, t, p)
        running_text += p["bytes_text"] + p["bytes_other"] + p["reply_bytes"]
        running_images += p["bytes_images"]
    assert tr["bytes_total"] == sum(t["bytes"] for t in turns)
    assert tr["bytes_total"] >= cmp["packet_delivery"]["bytes_total"]
    # the same fixture, the same invocation sequence, the same evidence set: only the delivery differs
    assert tr["invocations"] == cmp["packet_delivery"]["invocations"]


def test_tokens_are_reported_only_where_a_provider_measured_them(measured):
    project, cmp = measured
    pd = cmp["packet_delivery"]
    assert pd["tokens"]["input"] == {"kind": "unknown"} and pd["tokens"]["unknown_invocations"] == pd["invocations"]
    assert cmp["transcript_replay"]["tokens"]["input"] == {"kind": "unknown"}
    assert "never estimated" in cmp["transcript_replay"]["tokens"]["note"]


def test_preserved_evidence_coverage_and_withholding_are_counted_from_the_store(measured):
    project, cmp = measured
    store = project.store
    pres = cmp["preserved"]
    hashes = set()
    for p in store.list("packet"):
        for f in p.data["files"]:
            if f["role"] in ("reference", "render", "crop"):
                hashes.add(f["sha256"])
    assert pres["unique_evidence_files"] == len(hashes)
    assert pres["coverage_rows"] == len(store.list("coverage"))
    closed = [f for f in store.list("finding") if f.state == "closed"]
    assert pres["findings_closed_on_verified_renders"] == len([f for f in closed if f.data.get("after_render_ids")])
    assert pres["findings_total"] == len(store.list("finding"))
    withheld = [p for p in store.list("packet") if p.data.get("withheld")]
    assert pres["packets_with_withheld_material"] == len(withheld) >= 1
    # a shared transcript would have carried the builder's self-assessment into the review turn (R-11)
    exposed = cmp["transcript_replay"]["withheld_exposed"]
    assert exposed and any("self-assessment" in e["what"] for e in exposed)


def test_report_text_states_measurements_without_percentages_or_equivalence_claims(measured):
    project, cmp = measured
    text = harness.render_report(cmp)
    assert "%" not in text
    assert not re.search(r"\b(saving|savings|saves|equivalent|equally good|same quality)\b", text, re.IGNORECASE)
    assert "packet delivery" in text and "transcript replay" in text and "simulated" in text
    assert str(cmp["packet_delivery"]["bytes_total"]) in text and str(cmp["transcript_replay"]["bytes_total"]) in text
    assert "unknown" in text            # tokens with scripted seats
    assert "withheld" in text


def test_harness_cli_report_and_run_with_scripted_seats(workdir):
    runner = _fixture_runner()
    out = io.StringIO()
    code = cli.main(["harness", "run", str(workdir / "hx"), "--json"], runner_factory=lambda cfg, prj: runner, stdout=out)
    assert code == 0, out.getvalue()
    data = json.loads(out.getvalue())
    assert data["packet_delivery"]["bytes_total"] > 0 and data["transcript_replay"]["simulated"] is True
    wf = data["workflow_dir"]
    out2 = io.StringIO()
    code = cli.main(["harness", "report", wf], runner_factory=lambda cfg, prj: runner, stdout=out2)
    assert code == 0 and "packet delivery" in out2.getvalue() and "%" not in out2.getvalue()
    assert Path(wf, "harness-report.json").is_file() and Path(wf, "harness-report.txt").is_file()
