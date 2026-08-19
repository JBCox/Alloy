"""Session outcome records — the objective half of Alloy's feedback loop.

Supervisor Mode learns from finished conversations, and everything it learns is
only as trustworthy as what gets written here. Three rules shape this file, and
they are the whole design:

 1. ``hard_facts`` records only what STRUCTURALLY happened — who spoke how many
    times, which slash commands Josh ran, whether an [[ASK]] was answered,
    whether the run ended on the wrap token or hit the cap. Nothing in it is
    ever a judgement about quality. In particular Josh's plain interjections
    are counted, never sentiment-scored: sniffing "that's wrong" out of his
    text would smuggle a soft opinion into the one namespace that has to stay
    hard.
 2. Human opinion lives in ``human_feedback`` and nowhere else, it is always
    optional, and a missing rating is a fact about the UI (he was never asked,
    or skipped it), never evidence that a run went badly.
 3. A model's opinion lives in ``model_eval``. Kept separate on purpose: a
    model grading a conversation of models is the weakest signal in the file,
    and blending it into one score would launder that weakness invisibly.

Everything is derived from files a finished session already has
(``messages.jsonl`` + ``meta.json``), so an outcome can be rebuilt for any
session ever recorded — including ones that ran before this module existed.

This module deliberately imports NOTHING from relay: relay calls into it, and a
standalone reader keeps the aggregation side (``/retro``) testable without
dragging the engine — and its subprocess machinery — into a test process.
"""

import datetime
import json
import os
import time

OUTCOME_FILE = "outcome.json"
OUTCOME_VERSION = 1

MESSAGES_FILE = "messages.jsonl"
META_FILE = "meta.json"

RATINGS = ("helpful", "not_helpful", "skipped")
REASONS = ("incorrect", "incomplete", "inefficient", "poor_coordination")

# Josh's structurally-typed interventions. "interjection" is plain typed text:
# it is a fact that he stepped in, and nothing more is claimed about it.
COMMAND_KINDS = ("clear", "compact", "stop", "turns", "ceiling", "other")

# Walking a workspace that IS a real repo must stay bounded, and must never
# look like the session produced forty thousand files.
ARTIFACT_SCAN_MAX = 4000
ARTIFACT_NAMES_MAX = 25
ARTIFACT_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                      ".mypy_cache", ".pytest_cache", "sessions",
                      "attachments"}


# ---------------------------------------------------------------- readers

def _read_rows(session_dir):
    """Ordered message rows. A truncated final line (crash mid-append) is
    skipped, never fatal — same rule as relay.read_messages."""
    rows = []
    try:
        with open(os.path.join(session_dir, MESSAGES_FILE),
                  encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _read_meta(session_dir):
    try:
        with open(os.path.join(session_dir, META_FILE), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


# ------------------------------------------------------------- directives

def _trailing_directives(text, limit=4):
    """Names of the directives a reply ENDS with, outermost first.

    A deliberate mirror of relay.peel_directives' grammar, kept to the two
    rules bug history made load-bearing: end-anchored only (a reply that merely
    discusses the wrap token has text after it), and each peel anchors at the
    LAST '[[' so a stacked tail reads as two directives instead of one with a
    garbage argument.
    """
    names = []
    s = (text or "").rstrip()
    for _ in range(limit):
        if not s.endswith("]]"):
            break
        start = s.rfind("[[")
        if start < 0:
            break
        inner = s[start + 2:-2].strip()
        if not inner:
            break
        name = inner.split(":", 1)[0].split("|", 1)[0].strip().upper()
        if not name or not name.replace(" ", "").isalpha():
            break
        names.append(name)
        s = s[:start].rstrip()
    return names


def _command_kind(text):
    """Structural name of a slash command Josh ran. Never a quality signal."""
    word = (text or "").strip().lstrip("/").split()[:1]
    word = word[0].lower() if word else ""
    return word if word in COMMAND_KINDS else "other"


def classify_intervention(row):
    """One of: opener, ask_answer, command, interjection — or None.

    Purely structural: the row's speaker, its `meta` field and its round decide
    the kind. What Josh actually wrote is never inspected.
    """
    if not isinstance(row, dict) or row.get("speaker") != "josh":
        return None
    meta = (row.get("meta") or "").strip()
    if meta == "command":
        return "command"
    if meta.startswith("answer to"):
        return "ask_answer"
    if not row.get("round"):
        return "opener"
    return "interjection"


# ------------------------------------------------------------- artifacts

def _parse_ts(value):
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def workspace_artifacts(workspace, since_ts):
    """Files under `workspace` modified at or after `since_ts`.

    Best-effort and bounded: an unreadable folder, a vanished file or a giant
    repo must degrade to a smaller number, never raise and never stall a run.
    `truncated` is reported so a later reader can tell "25 files" from "at
    least 25 files" — a silent cap would read as completeness.
    """
    out = {"count": 0, "names": [], "truncated": False, "scanned": 0}
    if not workspace or since_ts is None or not os.path.isdir(workspace):
        return out
    scanned = 0
    names = []
    count = 0
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs
                   if d not in ARTIFACT_SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            scanned += 1
            if scanned > ARTIFACT_SCAN_MAX:
                out.update(count=count, names=names[:ARTIFACT_NAMES_MAX],
                           truncated=True, scanned=scanned)
                return out
            path = os.path.join(root, fn)
            try:
                if os.path.getmtime(path) + 1e-6 < since_ts:
                    continue
            except OSError:
                continue
            count += 1
            if len(names) < ARTIFACT_NAMES_MAX:
                names.append(os.path.relpath(path, workspace))
    out.update(count=count, names=names, scanned=scanned,
               truncated=count > len(names))
    return out


# ----------------------------------------------------------------- build

# run_rounds' own return value -> the recorded `ended` reason. The loop KNOWS
# how it ended, so when it tells us we believe it; inferring from the messages
# is the fallback for sessions rebuilt after the fact.
ENDED_FROM_LOOP = {"wrapped": "wrap", "stopped": "stop", "cap": "cap",
                   "fatal": "fatal"}


def build_outcome(session_dir, workspace=None, ended=None):
    """The hard-facts half of a session's outcome record."""
    rows = _read_rows(session_dir)
    meta = _read_meta(session_dir) or {}
    seats_meta = meta.get("seats") or []

    seats = []
    by_id = {}
    for s in seats_meta:
        if not isinstance(s, dict):
            continue
        entry = {"id": s.get("id"), "name": s.get("label"),
                 "provider": s.get("provider"), "model": s.get("model"),
                 "effort": s.get("effort"), "role": s.get("role"),
                 "turns": 0}
        seats.append(entry)
        if entry["id"] is not None:
            by_id[entry["id"]] = entry

    interventions = {"opener": 0, "interjection": 0, "ask_answer": 0,
                     "command": 0}
    commands = {k: 0 for k in COMMAND_KINDS}
    system_notes = []
    asked = answered = 0
    wrapped = False
    stopped = False
    first_ts = last_ts = None
    turns = 0

    for row in rows:
        ts = _parse_ts(row.get("ts"))
        if ts is not None:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        speaker = row.get("speaker")
        text = row.get("text") or ""
        if speaker == "system":
            if len(system_notes) < 20:
                system_notes.append(text[:200])
            continue
        kind = classify_intervention(row)
        if kind:
            interventions[kind] += 1
            if kind == "command":
                name = _command_kind(text)
                commands[name] += 1
                if name == "stop":
                    stopped = True
            elif kind == "ask_answer":
                answered += 1
            continue
        if speaker == "josh":
            continue
        # a seat's reply
        turns += 1
        if speaker in by_id:
            by_id[speaker]["turns"] += 1
            u = row.get("usage")
            if isinstance(u, dict):
                su = by_id[speaker].setdefault("usage", {
                    "cost_usd": None,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0
                })
                if isinstance(u.get("cost_usd"), (int, float)):
                    su["cost_usd"] = round((su["cost_usd"] or 0.0) + float(u["cost_usd"]), 6)
                su["input_tokens"] += int(u.get("input_tokens") or 0)
                su["output_tokens"] += int(u.get("output_tokens") or 0)
                su["total_tokens"] += int(u.get("total_tokens") or ((u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)))
        dirs = _trailing_directives(text)
        if "ASK" in dirs:
            asked += 1
        wrapped = "WRAP" in dirs

    if ended:
        ended = ENDED_FROM_LOOP.get(ended, ended)
    elif wrapped:
        ended = "wrap"
    elif stopped:
        ended = "stop"
    elif meta.get("until_done") and meta.get("turn_ceiling") \
            and meta.get("turn", 0) >= meta["turn_ceiling"]:
        ended = "ceiling"
    elif meta.get("max") and meta.get("rnd", 0) >= meta["max"]:
        ended = "cap"
    else:
        ended = "unknown"

    workspace = workspace or meta.get("workspace")
    if not workspace:
        guess = os.path.join(session_dir, "workspace")
        workspace = guess if os.path.isdir(guess) else None

    meta_usage = meta.get("usage")
    total_cost = None
    total_input = 0
    total_output = 0
    total_tokens = 0
    has_usage = False
    for s in seats:
        su = s.get("usage")
        if su:
            has_usage = True
            if su.get("cost_usd") is not None:
                total_cost = round((total_cost or 0.0) + su["cost_usd"], 6)
            total_input += su.get("input_tokens", 0)
            total_output += su.get("output_tokens", 0)
            total_tokens += su.get("total_tokens", 0)

    if has_usage:
        usage_fact = {
            "total_cost_usd": total_cost,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_tokens,
            "by_seat": {str(s["id"]): s["usage"] for s in seats if s.get("id") is not None and "usage" in s}
        }
    elif isinstance(meta_usage, dict):
        usage_fact = meta_usage
    else:
        usage_fact = None

    hard = {
        "turns": turns,
        "rounds_run": meta.get("rnd", 0),
        "rounds_max": meta.get("max"),
        "until_done": bool(meta.get("until_done")),
        "turn_ceiling": meta.get("turn_ceiling"),
        "mode": meta.get("mode"),
        "ended": ended,
        "seats": seats,
        "interventions": interventions,
        "interventions_total": sum(interventions.values()),
        "commands": commands,
        "asks": {"asked": asked, "answered": answered,
                 "unanswered": max(0, asked - answered)},
        "system_notes": {"count": len(system_notes), "samples": system_notes},
        "artifacts": workspace_artifacts(workspace, first_ts),
        "usage": usage_fact,
        "started": rows[0].get("ts") if rows else None,
        "ended_at": rows[-1].get("ts") if rows else None,
        "duration_s": (int(last_ts - first_ts)
                       if first_ts is not None and last_ts is not None
                       else None),
    }
    return {
        "outcome_version": OUTCOME_VERSION,
        "session": {"id": os.path.basename(os.path.abspath(session_dir)),
                    "workspace": workspace,
                    "parent": meta.get("parent")},
        "hard_facts": hard,
        "human_feedback": {"rating": None, "reasons": [], "note": "",
                           "ts": None},
        "model_eval": {},
    }


# ----------------------------------------------------------------- store

def _atomic_write(path, text):
    """os.replace with a retry: on Windows a concurrent reader without
    FILE_SHARE_DELETE blocks the rename with a transient PermissionError —
    the same hazard relay._atomic_write already learned about meta.json."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for delay in (0, 0.05, 0.15, 0.3):
        if delay:
            time.sleep(delay)
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            continue
    os.replace(tmp, path)


def read_outcome(session_dir):
    try:
        with open(os.path.join(session_dir, OUTCOME_FILE),
                  encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def write_outcome(session_dir, workspace=None, ended=None):
    """Rebuild hard_facts and persist, PRESERVING any feedback already there.

    Rebuilding is idempotent and safe to run after every continuation, which is
    the whole reason human opinion sits in its own namespace: recomputing the
    facts must never quietly erase a rating Josh gave an hour ago.
    Returns the record, or None if it could not be written.
    """
    try:
        rec = build_outcome(session_dir, workspace=workspace, ended=ended)
    except Exception:
        return None
    old = read_outcome(session_dir)
    if old:
        for key in ("human_feedback", "model_eval"):
            val = old.get(key)
            if isinstance(val, dict) and any(
                    v not in (None, "", [], {}) for v in val.values()):
                rec[key] = val
    try:
        _atomic_write(os.path.join(session_dir, OUTCOME_FILE),
                      json.dumps(rec, ensure_ascii=False, indent=1))
    except OSError:
        return None
    return rec


def set_feedback(session_dir, rating, reasons=None, note="", workspace=None):
    """Record Josh's end-card answer. `rating` must be one of RATINGS —
    "skipped" is a real, recorded answer meaning he was asked and declined,
    which is information; NO record at all means he was never asked."""
    if rating not in RATINGS:
        raise ValueError("rating must be one of %s" % (RATINGS,))
    reasons = list(reasons or [])
    bad = [r for r in reasons if r not in REASONS]
    if bad:
        raise ValueError("unknown reason(s): %s" % ", ".join(bad))
    rec = read_outcome(session_dir)
    if rec is None:
        rec = build_outcome(session_dir, workspace=workspace)
    rec["human_feedback"] = {
        "rating": rating, "reasons": reasons, "note": (note or "")[:2000],
        "ts": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        _atomic_write(os.path.join(session_dir, OUTCOME_FILE),
                      json.dumps(rec, ensure_ascii=False, indent=1))
    except OSError:
        return None
    return rec
