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
    print("18 passed, 0 failed")


if __name__ == "__main__":
    main()
