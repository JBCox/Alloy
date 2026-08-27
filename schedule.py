"""Scheduled rooms — when a saved room should start itself.

Standalone in the house style (export.py / fork.py / stats.py / memory.py):
stdlib only, imports nothing from relay or app, and no reader ever raises. It
owns NO root directory. relay's ``SESSIONS_DIR`` is the one answer to where
Alloy's data lives and callers pass the file path in, because a second
default is how two halves of an app come to disagree about where the data is
(fork.py's gotcha, and the reason ``write_tabs`` needs two globals redirected
in tests rather than one).

What this is NOT: a cron implementation. Four shapes cover what a desktop
harness is actually asked for — once, every N minutes, daily at a time, and
weekly on chosen days — and each is a handful of validated fields rather than
a mini-language whose parser is its own bug surface.

Four rules carry the module.

**A missed window is SKIPPED and said, never fired late.** Alloy is a desktop
app that is closed most nights. A 01:00 daily schedule found at 09:15 has one
honest answer and it is not "start now": the record keeps the miss, the app
says so, and the next occurrence is computed from NOW — so three days of
downtime produce zero runs, never three. ``MISSED_GRACE_S`` is the width of
"near enough": a poll that slipped, a laptop that slept for ten minutes.

**Daily and weekly are WALL-CLOCK; interval is elapsed time.** "Every day at
01:00" means the local clock reads 01:00, which on the two DST boundaries is
not 86400 seconds after the last one. Computing it by adding a day of seconds
would drift the job an hour twice a year, silently. "Every 90 minutes" means
90 minutes of elapsed time and has no wall-clock opinion at all. Two kinds of
arithmetic because they are two different questions.

**The acknowledgement is re-checked against the room's CURRENT grants at fire
time.** Rooms are saved by NAME and overwriting one is documented behaviour
(``relay.save_room``), so the room a schedule was acknowledged against can
gain Full access, connected apps or unattended desktop control months later
without the schedule being touched. A grant the ack does not cover stops the
fire and says which one. Narrowing is fine; widening is the whole point.

**Policy here, normalization in relay.** ``grants_for`` takes ALREADY
normalized axis values. Re-deriving them here would be a second copy of a
safety rule, which is exactly how ``browser_mcp._confine`` drifted four ways
from ``relay.confine_to_workspace`` under a docstring claiming parity.
"""

import datetime
import json
import os
import threading
import uuid

SCHEDULE_FILE = "schedules.json"          # a NAME, never a path
KINDS = ("once", "interval", "daily", "weekly")
SCHEDULES_MAX = 64
NAME_MAX = 80
PROMPT_MAX = 4000
TURNS_MIN, TURNS_MAX = 1, 500
MIN_INTERVAL_MIN = 5
MAX_INTERVAL_MIN = 7 * 24 * 60
# How late is still "on time". A poll slipped behind a busy emitter thread, a
# laptop that slept for a few minutes: fire. Anything older was a window Alloy
# was not there for, and is reported as missed.
MISSED_GRACE_S = 15 * 60
# datetime.weekday(): Monday is 0
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
RESULT_MAX = 400

# ---------------------------------------------------------------- grants --
# The standing grants a scheduled room hands out on every single run, in the
# words the acknowledgement uses. Order is display order.
GRANTS = (
    ("permission_full",
     "write, delete and run anything on this machine with no prompt "
     "(Full access)"),
    ("connectors",
     "reach your connected apps — Gmail, Drive, Calendar, M365, Epicor"),
    ("desktop_allowlist",
     "click and type in the desktop apps you allowlisted, without asking"),
    ("desktop_full",
     "click and type anywhere on your desktop, without asking"),
    ("browser_full",
     "browse, click, type and run scripts on the sites you allowlisted, "
     "without asking"),
    ("continuous_unbounded",
     "keep working with no spend cap, no time cap, and nothing allowed to "
     "stop it but you"),
)
GRANT_TEXT = dict(GRANTS)
GRANT_ORDER = [k for k, _ in GRANTS]


def grants_for(axes):
    """Which standing grants a room carries, in display order.

    `axes` is ALREADY normalized — see the module docstring. Keys:
    permission, connectors, desktop, browser, continuous_unbounded. Anything
    missing counts as absent, so an unknown shape claims nothing rather than
    guessing.
    """
    axes = axes if isinstance(axes, dict) else {}
    out = []
    if axes.get("permission") == "full":
        out.append("permission_full")
    if axes.get("connectors"):
        out.append("connectors")
    desktop = axes.get("desktop")
    if desktop == "allowlist":
        out.append("desktop_allowlist")
    elif desktop == "full":
        out.append("desktop_full")
    if axes.get("browser") == "full":
        out.append("browser_full")
    if axes.get("continuous_unbounded"):
        out.append("continuous_unbounded")
    return out


def grant_sentences(grants):
    """One readable line per grant, unknown keys dropped rather than shown
    raw — a key with no sentence is a bug here, not a thing to make Josh
    acknowledge."""
    return [GRANT_TEXT[g] for g in (grants or ()) if g in GRANT_TEXT]


def unattended_notes(axes):
    """Controls that mean nothing when nobody is watching, said out loud.

    Not grants: these are the opposite — controls that quietly do NOTHING (or
    worse) on a run started at 01:00. Stating a withholding rather than
    leaving it as an absence is the rule browser_mcp's WITHHELD list was
    written to, one surface over.

    The [[ASK]] one is measured, not guessed: `relay.ask_abort` gives an
    unanswered question a deadline in CONTINUOUS mode only, so a scheduled
    round-capped run whose seat asks Josh something waits until he presses
    Stop.
    """
    axes = axes if isinstance(axes, dict) else {}
    notes = []
    if axes.get("permission") == "ask":
        notes.append("Permission is set to Ask first. Nobody will be there to "
                     "answer, so each request waits and is then refused.")
    if axes.get("desktop") == "ask":
        notes.append("Desktop control is set to Ask. Every click a seat asks "
                     "for will be denied — the allowlist rung is the one that "
                     "works unattended.")
    if axes.get("browser") == "ask":
        notes.append("Browser control is set to Ask. Seats will be able to "
                     "read pages, but every interaction will be denied.")
    if axes.get("continuous") and axes.get("checkin_action") == "permission":
        notes.append("Check-ins are set to Ask permission, which makes the "
                     "run WAIT at every check-in until you answer.")
    if not axes.get("continuous"):
        notes.append("If a seat ends a reply with a question for you, the run "
                     "waits for an answer — only Keep Improving runs give an "
                     "unanswered question a deadline.")
    return notes


# ------------------------------------------------------------ time helpers --

def _fmt(dt):
    return dt.replace(microsecond=0).isoformat()


def parse_dt(text):
    """A stored stamp back into a naive local datetime, or None.

    Tolerant on the way in (a space instead of the T, a missing seconds
    field), exact on the way out — the store is written by `_fmt` and typed
    into by a human in the modal.
    """
    text = str(text or "").strip().replace(" ", "T")
    if not text:
        return None
    for shape in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.datetime.strptime(text, shape)
        except ValueError:
            continue
    return None


def parse_hhmm(text):
    """'01:00' -> (1, 0); anything else -> None. Deliberately strict: a time
    that silently became midnight is a nightly job firing at the wrong hour
    forever."""
    text = str(text or "").strip()
    if len(text) != 5 or text[2] != ":":
        return None
    try:
        hour, minute = int(text[:2]), int(text[3:])
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _at_time(day, hhmm):
    return datetime.datetime(day.year, day.month, day.day, hhmm[0], hhmm[1])


def next_occurrence(rec, after):
    """The first fire strictly AFTER `after`, or None if there is never one.

    Wall-clock for daily/weekly (see the module docstring on DST), elapsed
    seconds for interval, and a fixed instant for once — which is the only
    kind that can answer None, because a once that has passed has no next.
    """
    rec = rec if isinstance(rec, dict) else {}
    kind = rec.get("kind")
    if not isinstance(after, datetime.datetime):
        return None
    if kind == "once":
        when = parse_dt(rec.get("start"))
        return when if (when and when > after) else None
    if kind == "interval":
        try:
            every = int(rec.get("every_min") or 0)
        except (TypeError, ValueError):
            return None
        if every <= 0:
            return None
        return after + datetime.timedelta(minutes=every)
    hhmm = parse_hhmm(rec.get("at"))
    if hhmm is None:
        return None
    if kind == "daily":
        candidate = _at_time(after, hhmm)
        if candidate <= after:
            candidate = _at_time(after + datetime.timedelta(days=1), hhmm)
        return candidate
    if kind == "weekly":
        days = [d for d in (rec.get("days") or ()) if isinstance(d, int)]
        if not days:
            return None
        for step in range(0, 8):
            day = after + datetime.timedelta(days=step)
            if day.weekday() not in days:
                continue
            candidate = _at_time(day, hhmm)
            if candidate > after:
                return candidate
        return None
    return None


def describe(rec):
    """The recurrence as one short sentence — what the acknowledgement means
    by "how often it repeats", and what the list rows show."""
    rec = rec if isinstance(rec, dict) else {}
    kind = rec.get("kind")
    if kind == "once":
        when = parse_dt(rec.get("start"))
        return "Once, at %s" % (_fmt(when).replace("T", " ") if when
                                else "an invalid time")
    if kind == "interval":
        try:
            every = int(rec.get("every_min") or 0)
        except (TypeError, ValueError):
            every = 0
        if every and every % 60 == 0:
            hours = every // 60
            return "Every %d hours" % hours if hours > 1 else "Every hour"
        return "Every %d minutes" % every
    at = str(rec.get("at") or "??:??")
    if kind == "daily":
        return "Every day at %s" % at
    if kind == "weekly":
        days = [d for d in (rec.get("days") or ())
                if isinstance(d, int) and 0 <= d <= 6]
        names = ", ".join(WEEKDAYS[d] for d in sorted(set(days)))
        return "Every %s at %s" % (names or "never", at)
    return "Not scheduled"


# -------------------------------------------------------------- validation --

def normalize(spec, now=None):
    """One complete schedule record, or ValueError with a sentence.

    Rejects rather than sanitizes — the rule `relay.valid_skill_name` and
    `save_room` already follow: a field quietly repaired into something else
    is a nightly job doing something nobody asked for.
    """
    if not isinstance(spec, dict):
        raise ValueError("A schedule must be an object.")
    now = now or datetime.datetime.now()
    room = str(spec.get("room") or "").strip()
    if not room:
        raise ValueError("Pick a saved room for this schedule to start.")
    name = " ".join(str(spec.get("name") or "").split()).strip() or room
    if len(name) > NAME_MAX:
        raise ValueError("The schedule name must be at most %d characters."
                         % NAME_MAX)
    prompt = str(spec.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("Give the room something to do — the first message "
                         "is what starts the conversation.")
    if len(prompt) > PROMPT_MAX:
        raise ValueError("The first message must be at most %d characters."
                         % PROMPT_MAX)
    try:
        turns = int(spec.get("turns") or 10)
    except (TypeError, ValueError):
        raise ValueError("Rounds must be a whole number.")
    turns = max(TURNS_MIN, min(TURNS_MAX, turns))
    kind = str(spec.get("kind") or "").strip()
    if kind not in KINDS:
        raise ValueError("Unknown schedule kind %r — expected one of: %s."
                         % (kind, ", ".join(KINDS)))
    rec = {
        "id": str(spec.get("id") or "").strip() or uuid.uuid4().hex[:12],
        "name": name,
        "room": room,
        "prompt": prompt,
        "turns": turns,
        "kind": kind,
        "at": "",
        "days": [],
        "every_min": 0,
        "start": "",
        "enabled": bool(spec.get("enabled", True)),
        "next_run": "",
        "last_run": str(spec.get("last_run") or ""),
        "last_result": str(spec.get("last_result") or "")[:RESULT_MAX],
        "runs": int(spec.get("runs") or 0),
        "misses": int(spec.get("misses") or 0),
        "created": str(spec.get("created") or "") or _fmt(now),
        "ack": None,
    }
    if kind == "once":
        when = parse_dt(spec.get("start"))
        if when is None:
            raise ValueError("Give a date and time, like 2026-09-01 01:00.")
        if when <= now:
            raise ValueError("That time has already passed.")
        rec["start"] = _fmt(when)
    elif kind == "interval":
        try:
            every = int(spec.get("every_min") or 0)
        except (TypeError, ValueError):
            raise ValueError("The interval must be a whole number of minutes.")
        if not MIN_INTERVAL_MIN <= every <= MAX_INTERVAL_MIN:
            raise ValueError("The interval must be between %d minutes and %d "
                             "days." % (MIN_INTERVAL_MIN,
                                        MAX_INTERVAL_MIN // (24 * 60)))
        rec["every_min"] = every
    else:
        hhmm = parse_hhmm(spec.get("at"))
        if hhmm is None:
            raise ValueError("Give a time of day as HH:MM, like 01:00.")
        rec["at"] = "%02d:%02d" % hhmm
        if kind == "weekly":
            days = []
            for day in spec.get("days") or ():
                try:
                    day = int(day)
                except (TypeError, ValueError):
                    raise ValueError("Days must be numbers 0 (Mon) to 6 (Sun).")
                if not 0 <= day <= 6:
                    raise ValueError("Days must be numbers 0 (Mon) to 6 (Sun).")
                days.append(day)
            if not days:
                raise ValueError("Pick at least one day of the week.")
            rec["days"] = sorted(set(days))
    ack = spec.get("ack")
    if isinstance(ack, dict):
        granted = [g for g in (ack.get("grants") or ()) if g in GRANT_TEXT]
        rec["ack"] = {"grants": sorted(set(granted), key=GRANT_ORDER.index),
                      "at": str(ack.get("at") or "") or _fmt(now)}
    nxt = parse_dt(spec.get("next_run"))
    if nxt is None or nxt <= now:
        nxt = next_occurrence(rec, now)
    rec["next_run"] = _fmt(nxt) if nxt else ""
    return rec


def ack_gap(rec, grants):
    """The standing grants this schedule was never acknowledged for.

    Compared against the room's grants NOW, never against the ones stored on
    the record: rooms are saved by name and overwriting one is documented
    behaviour, so an ack from March cannot speak for a room that gained Full
    access in August. Narrowing is silent; widening is the whole point.
    """
    have = set((rec.get("ack") or {}).get("grants") or ()) \
        if isinstance(rec, dict) else set()
    return [g for g in (grants or ()) if g not in have]


def ack_covers(rec, grants):
    return not ack_gap(rec, grants)


# ------------------------------------------------------------------ store --
# One JSON file, read-modify-written by two threads (the bridge saves edits,
# the poller records fires). A single process lock is enough for THAT. Two
# Alloy windows polling one file is a real case this does not close: `claim`'s
# compare-and-set narrows the window to the milliseconds between one process's
# read and its rename, and the honest statement is that schedules assume one
# window, not that they are safe across several.
_LOCK = threading.RLock()


def read_schedules(path):
    """{"version": 1, "schedules": [...]}, always well-formed.

    Never raises: a corrupt or missing file means "nothing scheduled", which
    degrades to exactly the behaviour that existed before schedules.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {"version": 1, "schedules": []}
    rows, seen = [], set()
    raw = data.get("schedules") if isinstance(data, dict) else None
    for row in raw or ():
        if not isinstance(row, dict):
            continue
        try:
            rec = _rehydrate(row)
        except Exception:
            continue
        if not rec["id"] or rec["id"] in seen:
            continue
        seen.add(rec["id"])
        rows.append(rec)
        if len(rows) >= SCHEDULES_MAX:
            break
    return {"version": 1, "schedules": rows}


def _rehydrate(row):
    """A stored row back into the full shape without re-validating it.

    Deliberately NOT `normalize`: normalize refuses a `once` whose time has
    passed, which is right for a NEW schedule and would silently delete the
    record of one that already fired. A reader must never lose data a writer
    was allowed to store.
    """
    days = [int(d) for d in (row.get("days") or ())
            if isinstance(d, int) and 0 <= d <= 6]
    ack = row.get("ack")
    if isinstance(ack, dict):
        granted = [g for g in (ack.get("grants") or ()) if g in GRANT_TEXT]
        ack = {"grants": sorted(set(granted), key=GRANT_ORDER.index),
               "at": str(ack.get("at") or "")}
    else:
        ack = None
    try:
        turns = int(row.get("turns") or 10)
    except (TypeError, ValueError):
        turns = 10
    try:
        every = int(row.get("every_min") or 0)
    except (TypeError, ValueError):
        every = 0
    return {
        "id": str(row.get("id") or "").strip(),
        "name": str(row.get("name") or ""),
        "room": str(row.get("room") or ""),
        "prompt": str(row.get("prompt") or ""),
        "turns": max(TURNS_MIN, min(TURNS_MAX, turns)),
        "kind": row.get("kind") if row.get("kind") in KINDS else "daily",
        "at": str(row.get("at") or ""),
        "days": sorted(set(days)),
        "every_min": every,
        "start": str(row.get("start") or ""),
        "enabled": bool(row.get("enabled")),
        "next_run": str(row.get("next_run") or ""),
        "last_run": str(row.get("last_run") or ""),
        "last_result": str(row.get("last_result") or "")[:RESULT_MAX],
        "runs": int(row.get("runs") or 0) if str(row.get("runs") or 0).lstrip("-").isdigit() else 0,
        "misses": int(row.get("misses") or 0) if str(row.get("misses") or 0).lstrip("-").isdigit() else 0,
        "created": str(row.get("created") or ""),
        "ack": ack,
    }


def _write(path, rows):
    data = {"version": 1, "schedules": list(rows)[:SCHEDULES_MAX]}
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return data


def _sorted(rows):
    """Soonest first; disarmed and finished schedules last. `next_run` is an
    ISO string, so a plain string sort is a time sort, and an empty one has
    to sort LAST rather than first — which is what the sentinel is for."""
    return sorted(rows, key=lambda r: (r.get("next_run") or "9999",
                                       r.get("name") or ""))


def save_schedule(path, spec, grants=(), now=None):
    """Create or replace ONE schedule. Raises ValueError with a sentence.

    `grants` is the room's CURRENT standing grants; a schedule whose ack does
    not cover them is refused HERE rather than saved-and-never-fired, because
    a row that looks armed and silently never runs is the worst of the three
    possible answers.
    """
    rec = normalize(spec, now=now)
    gap = ack_gap(rec, grants)
    if gap:
        raise ValueError(
            "This room grants standing access that has not been acknowledged "
            "for this schedule: %s." % "; ".join(grant_sentences(gap)))
    with _LOCK:
        rows = [r for r in read_schedules(path)["schedules"]
                if r["id"] != rec["id"]]
        if len(rows) >= SCHEDULES_MAX:
            raise ValueError("There are already %d schedules." % SCHEDULES_MAX)
        rows.append(rec)
        _write(path, _sorted(rows))
    return rec


def delete_schedule(path, sched_id):
    """Remove one. A missing id is a clean False — deleting something already
    gone must never surface as an error."""
    with _LOCK:
        rows = read_schedules(path)["schedules"]
        keep = [r for r in rows if r["id"] != sched_id]
        if len(keep) == len(rows):
            return False
        _write(path, keep)
    return True


def set_enabled(path, sched_id, on, now=None):
    """Arm or disarm one schedule. Re-arming recomputes `next_run` from NOW —
    a schedule switched back on must not inherit a window that passed while it
    was off, which would fire it on the very next poll."""
    now = now or datetime.datetime.now()
    with _LOCK:
        rows = read_schedules(path)["schedules"]
        hit = None
        for rec in rows:
            if rec["id"] == sched_id:
                hit = rec
                break
        if hit is None:
            return None
        hit["enabled"] = bool(on)
        if on:
            nxt = parse_dt(hit["next_run"])
            if nxt is None or nxt <= now:
                nxt = next_occurrence(hit, now)
            hit["next_run"] = _fmt(nxt) if nxt else ""
        _write(path, _sorted(rows))
    return dict(hit)


def due(rows, now):
    """Every armed schedule whose window has arrived, soonest first."""
    out = []
    for rec in rows or ():
        if not isinstance(rec, dict) or not rec.get("enabled"):
            continue
        when = parse_dt(rec.get("next_run"))
        if when is not None and when <= now:
            out.append((when, rec))
    out.sort(key=lambda pair: pair[0])
    return [rec for _, rec in out]


def fire_verdict(rec, now):
    """"run" or "missed" for a due schedule, and the sentence for a miss.

    A window Alloy was not there for is never fired late: see the module
    docstring. The next occurrence is computed from NOW in BOTH cases, which
    is what turns three days of downtime into zero runs instead of three.
    """
    when = parse_dt((rec or {}).get("next_run"))
    if when is None:
        return "run", ""
    late = (now - when).total_seconds()
    if late <= MISSED_GRACE_S:
        return "run", ""
    return "missed", ("Missed %s — Alloy was not running."
                      % _fmt(when).replace("T", " "))


def claim(path, sched_id, expected_next_run, now=None):
    """Take ownership of one due fire, advancing the record FIRST.

    Advancing before the run starts is the rule `run_checkin` already follows
    (mark the check taken before the side call): a fire recorded afterwards
    is a fire that repeats at every poll for as long as the conversation
    lasts. `expected_next_run` is a compare-and-set, so a schedule another
    poll already advanced is not claimed twice.

    Returns (record, verdict, note); (None, "", "") when it was not claimable.
    """
    now = now or datetime.datetime.now()
    with _LOCK:
        rows = read_schedules(path)["schedules"]
        hit = None
        for rec in rows:
            if rec["id"] == sched_id:
                hit = rec
                break
        if hit is None or not hit["enabled"]:
            return None, "", ""
        if hit["next_run"] != expected_next_run:
            return None, "", ""
        verdict, note = fire_verdict(hit, now)
        nxt = next_occurrence(hit, now)
        hit["next_run"] = _fmt(nxt) if nxt else ""
        if not hit["next_run"]:
            # a `once` has nothing left to do; leaving it armed would make the
            # list promise a run that can never come
            hit["enabled"] = False
        hit["last_run"] = _fmt(now)
        if verdict == "missed":
            hit["misses"] += 1
            hit["last_result"] = note[:RESULT_MAX]
        else:
            hit["last_result"] = "starting…"
        _write(path, _sorted(rows))
        return dict(hit), verdict, note


def record_result(path, sched_id, text, ran=False, now=None):
    """Write the outcome of one fire onto its record. Silent on a missing id:
    a schedule Josh deleted while its run was starting is not an error."""
    now = now or datetime.datetime.now()
    with _LOCK:
        rows = read_schedules(path)["schedules"]
        for rec in rows:
            if rec["id"] != sched_id:
                continue
            rec["last_result"] = str(text or "")[:RESULT_MAX]
            rec["last_run"] = _fmt(now)
            if ran:
                rec["runs"] += 1
            _write(path, _sorted(rows))
            return dict(rec)
    return None
