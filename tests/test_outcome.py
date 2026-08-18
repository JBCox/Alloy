"""Session outcome records — token-free. Run: python tests/test_outcome.py"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import outcome  # noqa: E402

PASS = FAIL = 0


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("  FAIL:", label)


def eq(got, want, label):
    ok(got == want, "%s (got %r, want %r)" % (label, got, want))


def make_session(rows, meta=None, dirname=None):
    d = dirname or tempfile.mkdtemp(prefix="alloy-outcome-")
    with open(os.path.join(d, outcome.MESSAGES_FILE), "w",
              encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    if meta is not None:
        with open(os.path.join(d, outcome.META_FILE), "w",
                  encoding="utf-8") as f:
            json.dump(meta, f)
    return d


def row(speaker, text, *, name=None, meta="", round=1, ts="2026-08-17T10:00:00",
        provider=None):
    return {"speaker": speaker, "name": name or speaker, "text": text,
            "meta": meta, "round": round, "ts": ts, "provider": provider}


SEATS = [{"id": "s1", "label": "Claude", "provider": "claude",
          "model": "claude-opus-5", "effort": "high", "role": None},
         {"id": "s2", "label": "GPT", "provider": "gpt",
          "model": "gpt-5.6-sol", "effort": "high", "role": "Skeptic"}]


# --------------------------------------------------- trailing directives

def test_directives():
    d = outcome._trailing_directives
    eq(d("all done. [[WRAP]]"), ["WRAP"], "wrap at end of a sentence")
    eq(d("[[WRAP]] is how you end a chat"), [], "wrap discussed, not played")
    eq(d("over to you [[NEXT: GPT]] [[WRAP]]"), ["WRAP", "NEXT"],
       "stacked tail peels as two, last-[[ anchored")
    eq(d("q? [[ASK: pick one | a | b]]"), ["ASK"], "ask with options")
    eq(d("`[[WRAP]]`"), [], "code-span mention does not count")
    eq(d(""), [], "empty text")
    eq(d(None), [], "None text")


# ------------------------------------------------ intervention typing

def test_classify():
    c = outcome.classify_intervention
    eq(c(row("josh", "start here", round=0)), "opener", "round 0 = opener")
    eq(c(row("josh", "/clear gpt", meta="command")), "command", "slash command")
    eq(c(row("josh", "option 1", meta="answer to Claude")), "ask_answer",
       "ask answer")
    eq(c(row("josh", "actually try X")), "interjection", "plain interjection")
    eq(c(row("s1", "hello")), None, "a seat is not an intervention")
    eq(c(row("system", "note")), None, "a relay note is not an intervention")
    eq(c("not a dict"), None, "garbage input")
    # the load-bearing rule: no sentiment is ever inferred from his words
    eq(c(row("josh", "no, that is completely wrong")), "interjection",
       "negative-sounding text is still just an interjection")


def test_command_kinds():
    eq(outcome._command_kind("/compact claude"), "compact", "compact")
    eq(outcome._command_kind("/stop"), "stop", "stop")
    eq(outcome._command_kind("/turns 4"), "turns", "turns")
    eq(outcome._command_kind("/help"), "other", "unknown command buckets")
    eq(outcome._command_kind(""), "other", "empty")


# ------------------------------------------------------------- build

def test_build_counts():
    rows = [row("josh", "kick off", round=0),
            row("s1", "hi", name="Claude"),
            row("s2", "hello", name="GPT"),
            row("josh", "/compact", meta="command"),
            row("s1", "more", name="Claude"),
            row("system", "Claude failed twice; skipping"),
            row("josh", "steer this way"),
            row("s2", "done here. [[WRAP]]", name="GPT")]
    d = make_session(rows, {"seats": SEATS, "rnd": 3, "max": 6,
                            "mode": "moderator"})
    rec = outcome.build_outcome(d)
    hf = rec["hard_facts"]
    eq(hf["turns"], 4, "seat turns counted")
    eq([s["turns"] for s in hf["seats"]], [2, 2], "per-seat turns by slot id")
    eq(hf["interventions"], {"opener": 1, "interjection": 1, "ask_answer": 0,
                             "command": 1}, "typed interventions")
    eq(hf["interventions_total"], 3, "intervention total")
    eq(hf["commands"]["compact"], 1, "compact counted")
    eq(hf["system_notes"]["count"], 1, "relay note captured")
    eq(hf["ended"], "wrap", "ended on the wrap token")
    eq(hf["mode"], "moderator", "mode carried from meta")
    eq(rec["outcome_version"], outcome.OUTCOME_VERSION, "version stamped")
    eq(rec["human_feedback"]["rating"], None, "feedback starts empty")
    eq(rec["model_eval"], {}, "model_eval starts empty")


def test_ended_reasons():
    seats = {"seats": SEATS}
    d = make_session([row("s1", "still going", name="Claude")],
                     dict(seats, rnd=6, max=6))
    eq(outcome.build_outcome(d)["hard_facts"]["ended"], "cap", "hit round cap")

    d = make_session([row("s1", "a", name="Claude"),
                      row("josh", "/stop", meta="command")],
                     dict(seats, rnd=2, max=6))
    eq(outcome.build_outcome(d)["hard_facts"]["ended"], "stop", "Josh stopped")

    d = make_session([row("s1", "a", name="Claude")],
                     dict(seats, until_done=True, turn_ceiling=10, turn=10))
    eq(outcome.build_outcome(d)["hard_facts"]["ended"], "ceiling",
       "until-done hit its ceiling")

    d = make_session([row("s1", "a", name="Claude")], dict(seats, rnd=2, max=6))
    eq(outcome.build_outcome(d)["hard_facts"]["ended"], "unknown",
       "unfinished run claims nothing")


def test_asks():
    rows = [row("s1", "which? [[ASK: pick | a | b]]", name="Claude"),
            row("josh", "a", meta="answer to Claude"),
            row("s2", "and this? [[ASK: another]]", name="GPT")]
    d = make_session(rows, {"seats": SEATS})
    eq(outcome.build_outcome(d)["hard_facts"]["asks"],
       {"asked": 2, "answered": 1, "unanswered": 1}, "asked/answered split")


def test_robustness():
    d = tempfile.mkdtemp(prefix="alloy-outcome-")
    rec = outcome.build_outcome(d)          # no files at all
    eq(rec["hard_facts"]["turns"], 0, "empty session dir builds")
    eq(rec["hard_facts"]["seats"], [], "no seats without meta")

    # a crash mid-append leaves a truncated final line
    d = make_session([row("s1", "ok", name="Claude")], {"seats": SEATS})
    with open(os.path.join(d, outcome.MESSAGES_FILE), "a",
              encoding="utf-8") as f:
        f.write('{"speaker": "s1", "text": "trunc')
    eq(outcome.build_outcome(d)["hard_facts"]["turns"], 1,
       "truncated last line skipped, not fatal")

    d = make_session([row("s1", "ok", name="Claude")], {"seats": "garbage"})
    eq(outcome.build_outcome(d)["hard_facts"]["seats"], [],
       "garbage meta degrades to empty")


# --------------------------------------------------------- artifacts

def test_artifacts():
    ws = tempfile.mkdtemp(prefix="alloy-ws-")
    old = os.path.join(ws, "already-here.txt")
    with open(old, "w") as f:
        f.write("x")
    os.utime(old, (1000, 1000))             # long before the session
    os.makedirs(os.path.join(ws, ".git"), exist_ok=True)
    with open(os.path.join(ws, ".git", "config"), "w") as f:
        f.write("noise")
    with open(os.path.join(ws, "made.txt"), "w") as f:
        f.write("new")

    got = outcome.workspace_artifacts(ws, 2000)
    eq(got["count"], 1, "only files newer than session start")
    eq(got["names"], ["made.txt"], "the new file is named")
    eq(outcome.workspace_artifacts(ws, None)["count"], 0, "no start ts")
    eq(outcome.workspace_artifacts(os.path.join(ws, "nope"), 2000)["count"], 0,
       "missing workspace degrades quietly")


# ------------------------------------------------------- persistence

def test_write_and_feedback():
    d = make_session([row("s1", "hi", name="Claude")], {"seats": SEATS})
    rec = outcome.write_outcome(d)
    ok(rec is not None, "write_outcome returns the record")
    ok(os.path.isfile(os.path.join(d, outcome.OUTCOME_FILE)),
       "outcome.json lands in the session dir")
    eq(outcome.read_outcome(d)["hard_facts"]["turns"], 1, "read back")

    outcome.set_feedback(d, "not_helpful", ["incomplete"], "ran out of rounds")
    eq(outcome.read_outcome(d)["human_feedback"]["rating"], "not_helpful",
       "rating stored")
    eq(outcome.read_outcome(d)["human_feedback"]["reasons"], ["incomplete"],
       "reasons stored")
    ok(outcome.read_outcome(d)["human_feedback"]["ts"], "feedback is stamped")

    # THE contract for the end card: rebuilding facts must not erase opinion
    with open(os.path.join(d, outcome.MESSAGES_FILE), "a",
              encoding="utf-8") as f:
        f.write(json.dumps(row("s2", "more", name="GPT")) + "\n")
    rec = outcome.write_outcome(d)
    eq(rec["hard_facts"]["turns"], 2, "facts rebuilt on rewrite")
    eq(rec["human_feedback"]["rating"], "not_helpful",
       "human feedback survives a rebuild")

    try:
        outcome.set_feedback(d, "amazing")
        ok(False, "bad rating should raise")
    except ValueError:
        ok(True, "bad rating rejected")
    try:
        outcome.set_feedback(d, "helpful", ["vibes"])
        ok(False, "bad reason should raise")
    except ValueError:
        ok(True, "unknown reason rejected")

    # skipped is a real answer, distinct from never having been asked
    outcome.set_feedback(d, "skipped")
    eq(outcome.read_outcome(d)["human_feedback"]["rating"], "skipped",
       "skip is recorded, not absent")


def main():
    for fn in (test_directives, test_classify, test_command_kinds,
               test_build_counts, test_ended_reasons, test_asks,
               test_robustness, test_artifacts, test_write_and_feedback):
        print("--", fn.__name__)
        fn()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
