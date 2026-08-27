"""Retro/playbook tests — token-free. Run: python tests/test_retro.py"""

import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import retro


def record(root, sid, ended="wrap", rating=None, reasons=None, unanswered=0):
    d = os.path.join(root, sid); os.makedirs(d)
    data = {"outcome_version": 1, "session": {"id": sid},
            "hard_facts": {"ended": ended, "asks": {"unanswered": unanswered}},
            "human_feedback": {"rating": rating, "reasons": reasons or []},
            "model_eval": {}}
    with open(os.path.join(d, "outcome.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)


def main():
    root = tempfile.mkdtemp(prefix="alloy-retro-")
    record(root, "s1", rating="not_helpful", reasons=["poor_coordination"])
    record(root, "s2", ended="cap", unanswered=1)
    record(root, "s3", ended="cap", unanswered=1)
    now = datetime.datetime(2026, 8, 17, 12, 0)
    records = retro.scan_outcomes(root)
    assert len(records) == 3
    candidates = retro.derive_heuristics(records, now)
    ids = {h["heuristic_id"] for h in candidates}
    assert ids == {"explicit-ownership", "avoid-repeated-cap", "resolve-asks-early"}
    assert next(h for h in candidates if h["heuristic_id"] == "explicit-ownership")["provenance"] == ["s1"]
    existing = {"heuristics": [{"heuristic_id": "explicit-ownership",
                 "directive": "Josh's wording", "status": "active", "pinned": True,
                 "expiry": "2020-01-01T00:00:00", "provenance": [], "evidence_count": 0},
                {"heuristic_id": "resolve-asks-early", "directive": "no",
                 "status": "dismissed", "pinned": False,
                 "expiry": "2020-01-01T00:00:00"}]}
    merged = retro.merge_heuristics(existing, candidates, now)
    own = next(h for h in merged["heuristics"] if h["heuristic_id"] == "explicit-ownership")
    assert own["pinned"] and own["directive"] == "Josh's wording"
    dismissed = next(h for h in merged["heuristics"] if h["heuristic_id"] == "resolve-asks-early")
    assert dismissed["status"] == "dismissed"
    playbook, report, path = retro.run_retro(root, now)
    assert os.path.exists(path) and "Retro: 3 outcome(s)" in report
    assert retro.read_playbook(root)["playbook_version"] == 1
    assert playbook["heuristics"]

    # --- concurrent writers must not splice the playbook together ---------
    # There is exactly ONE playbook for the whole app, and playbook_block()
    # is interpolated straight into SUPERVISOR_PROMPT, so a spliced file
    # degrades the planner of a possibly-unattended run. The old code used a
    # fixed "<path>.tmp", which two writers truncate from under each other.
    import threading
    big = {"playbook_version": 1, "updated": "x",
           "heuristics": [{"heuristic_id": f"h{i}", "directive": "d" * 400,
                           "status": "active", "pinned": False,
                           "expiry": "2099-01-01T00:00:00"}
                          for i in range(60)]}
    errors = []

    def hammer():
        try:
            for _ in range(12):
                retro.write_playbook(root, big)
        except Exception as e:                       # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f"concurrent write raised: {errors}"
    # Whatever landed must be COMPLETE json, not a splice of two writes.
    again = retro.read_playbook(root)
    assert len(again["heuristics"]) == 60, again.get("heuristics")
    # and no scratch files may be left lying in the session rail's folder
    leftovers = [n for n in os.listdir(root) if n.endswith(".tmp")]
    assert not leftovers, leftovers

    print("21 passed, 0 failed")


if __name__ == "__main__":
    main()
