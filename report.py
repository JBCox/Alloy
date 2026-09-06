"""What happened while you were away — one card for a chat that ran without you.

Standalone in the house style (export.py / fork.py / stats.py / memory.py /
schedule.py): stdlib only, imports nothing from relay or app, owns no root
directory and no path, and no public function ever raises whatever it is
handed. It reads ONE already-loaded ``meta.json`` dict; it opens no file,
starts no subprocess and knows nothing about git.

**Policy here, normalization in relay.** ``build`` takes a ``facts`` dict of
values relay has ALREADY computed with its own functions — the supervisor
status word, whether the run was interrupted, whether it is live right now,
and the trace cap. Re-deriving any of them here would be a second copy of a
rule that already exists (``supervisor_status``'s precedence is documented as
the one a re-deriving reader gets wrong), which is exactly how
``browser_mcp._confine`` drifted four ways from ``relay.confine_to_workspace``
under a docstring claiming parity.

Five rules carry the module, and four of them come straight off measurements
of the real ``sessions/`` folder rather than from the feature request.

**A count taken from the trace is a FLOOR, and says so.** ``supervisor_trace``
keeps ``SUPERVISOR_TRACE_MAX`` entries and trims the OLDEST
(``del entries[:-MAX]``). Measured: a two-hour run already sits at 101 of 120,
so an overnight run loses its early waves — and it loses them in the one
direction that erases the night. Every trace-derived number here is published
with ``floor: True`` when the record was trimmed, and the card renders "at
least N". Truncation announcing itself is the house rule; the trap is that
this particular truncation gets worse exactly as the run gets more worth
reporting on.

**Waves are counted from ``plan_created``, never from a wave NUMBER.**
``supervisor_wave_index`` is a CURSOR, not a counter: ``plan_workstreams``
resets it to 1 on every fresh plan. Measured: run ``20260823-165418`` dispatched
at least six plans across two hours and its meta reads
``supervisor_wave_index: 1``, with the trace's own ``wave`` field running
1,1 → 2,2 → 1 → 3,3 → 4,4,4 → 5,5 → 1,1,1. So both the meta field and
``max(entry["wave"])`` under-report, and neither is a count of anything.

**A number nobody reported is None, never 0.** ``usage`` is absent from 34 of
52 real sessions — including BOTH of the longest runs — and Gemini and
OpenCode seats report no cost at all even when it is present. A ``$0.00`` in a
spend column reads as "this was free" rather than "nobody said", so an
unreported cost stays ``None`` all the way to a dimmed dash, and ``reported``
says whether anything was measured at all.

**Tokens are deliberately absent, and the card says so.** GPT's counters were
thread-CUMULATIVE and summed until the 2026-08-27 fix, so the records on disk
still carry the lie (the real folder aggregates to 559 million input tokens if
believed), and the two providers that do report cached tokens use opposite
conventions. ``stats.py`` is the one reader that handles both correctly, via
``MIN_TRUSTED_BASIS`` and ``CACHE_CONVENTION``. Publishing a token figure here
would be a second, wrong copy of that; cost and turn counts are unaffected by
the basis bug, so those are what this reports.

**Nothing here is inferred.** Every field is either something the record
states or an absence with a named reason. ``notes`` carries the withholdings
as sentences rather than leaving them as missing keys — a card that silently
omits a row reads as "there was nothing to say", which is a claim.
"""

import datetime

# Auto-show threshold for a run whose provenance is not recorded. A chat that
# ran twenty minutes is one you walked away from; a two-minute one you watched.
# Only consulted when `started_by` does not already settle it as a FACT.
AWAY_MIN_S = 20 * 60

DELIVERED_MAX = 24          # files named on the card before "+N more"
OBJECTIVES_MAX = 12         # settled objectives listed, newest last
TAIL_MAX = 1400             # chars of a failing gate's output kept
GOAL_MAX = 400
SEATS_MAX = 24

# Trace types this module counts, and NOTHING else is counted.
#
# The membership rule is not "is this event interesting" but "does exactly one
# occurrence of the thing produce exactly one entry". Measured, by counting
# `supervisor_trace` call sites per phase in relay.py:
#
#   plan_created (1 site)  · run_revived (1) · limit_reached (1)
#   goal_accepted (1)      · goal_unresolved (1)      -> countable
#   supervisor_error (15 sites, but one entry per failure) -> countable
#   gate_result (4 sites: running / passed / failed / commit-binding)
#                                                     -> countable BY STATUS
#   health_check (8 sites, 2-3 entries per check-in)  -> NOT countable
#   objective_set (4 sites, several per rollover)     -> NOT countable
#
# So there is deliberately no check-in counter and no rollover counter here.
# Settled objectives are read from `continuous.history`, which archive_objective
# appends exactly once per objective — the direct record, not an inference over
# a log. A number that cannot be derived honestly is not published; the
# alternative was matching our own entry TITLES, which is one wording change
# away from silently becoming wrong.
TYPE_PLAN = "plan_created"
TYPE_GATE = "gate_result"
TYPE_ERROR = "supervisor_error"
TYPE_REVIVED = "run_revived"
TYPE_LIMIT = "limit_reached"
TYPE_ACCEPTED = "goal_accepted"
TYPE_UNRESOLVED = "goal_unresolved"


def _d(value):
    """A dict, whatever arrived. Rows come off disk and may be anything."""
    return value if isinstance(value, dict) else {}


def _l(value):
    """A list, whatever arrived."""
    return value if isinstance(value, list) else []


def _s(value, limit=None):
    """A string, whatever arrived; never the word 'None'."""
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:limit] if limit else text


def _n(value):
    """An int, or None when the value is not a number. A missing count is
    NOT zero anywhere in this module."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _when(text):
    """A naive datetime from one of meta's ISO stamps, or None.

    Every stamp Alloy writes is naive local (``datetime.now().isoformat``),
    so they are compared to each other and to a caller-supplied ``now``,
    never to UTC.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return datetime.datetime.fromisoformat(text.strip())
    except ValueError:
        return None


def human_duration(seconds):
    """'2h 1m' / '14m' / '48s', or '' when there is no measurement.

    Public because the card and any future renderer must word a duration the
    same way; '' rather than '0s' for None, since a duration nobody measured
    is not a duration of zero.
    """
    total = _n(seconds)
    if total is None or total < 0:
        return ""
    if total < 60:
        return "%ds" % total
    minutes, hours = (total // 60) % 60, total // 3600
    if hours:
        return "%dh %dm" % (hours, minutes) if minutes else "%dh" % hours
    return "%dm" % minutes


def _trimmed(entries, cap):
    """Has the trace lost its beginning?

    Two independent signals, because either alone is wrong in a way that
    matters. The cap comparison is exact while the caller's ``cap`` matches
    the engine's, and says nothing if that constant ever moves. The
    structural signal does not care about the constant: a complete trace
    begins with the ``plan_started`` of the run's first plan, so a first
    entry that is anything else is proof of trimming — and it keeps working
    if the cap is raised, lowered or removed.

    Either signal is enough. This deliberately over-reports rather than
    under-reports: "at least 6 waves" when there were exactly 6 costs Josh
    nothing, while "6 waves" when there were 14 is the lie the whole rule
    exists to prevent.
    """
    rows = [r for r in entries if isinstance(r, dict)]
    if not rows:
        return False
    limit = _n(cap)
    if limit is not None and limit > 0 and len(rows) >= limit:
        return True
    return _s(rows[0].get("type")) != "plan_started"


def _seat_names(meta):
    """{seat id as string: display name} from the roster, for the spend rows."""
    out = {}
    for seat in _l(meta.get("seats"))[:SEATS_MAX]:
        row = _d(seat)
        key = _s(row.get("id"))
        if not key:
            continue
        out[key] = (_s(row.get("label")) or _s(row.get("provider"))
                    or ("Seat " + key), _s(row.get("provider")))
    return out


def _spend(meta):
    """Cost and turns per seat — and an honest blank when nobody reported.

    ``by_seat`` entries whose ``cost_usd`` is None are seats whose CLI
    reports no cost at all (Gemini, OpenCode). They keep their row, because
    they DID take turns and hiding them would understate the roster, but
    their cost stays None so the renderer can draw a dash rather than a zero.
    """
    usage = _d(meta.get("usage"))
    names = _seat_names(meta)
    rows, any_cost = [], False
    for key, value in sorted(_d(usage.get("by_seat")).items()):
        seat = _d(value)
        cost = seat.get("cost_usd")
        cost = float(cost) if isinstance(cost, (int, float)) \
            and not isinstance(cost, bool) else None
        if cost is not None:
            any_cost = True
        name, provider = names.get(_s(key), (_s(key) and "Seat " + _s(key), ""))
        rows.append({"id": _s(key), "name": name, "provider": provider,
                     "cost_usd": cost, "turns": _n(seat.get("turns"))})
    side = {}
    for kind, value in _d(usage.get("by_kind")).items():
        cost = _d(value).get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            side[_s(kind)] = float(cost)
            any_cost = True
    total = usage.get("total_cost_usd")
    total = float(total) if isinstance(total, (int, float)) \
        and not isinstance(total, bool) else None
    # `record_usage` SEEDS `total_cost_usd: 0.0` the first time it is called,
    # whatever the turn reported — so the total's mere presence proves
    # nothing, and an earlier version read it as proof and chipped "$0.00
    # spent" on a Gemini/OpenCode room directly above a Spend row saying
    # "no cost reported". The two halves of one card contradicting each
    # other is the exact failure the blank-not-zero rule exists to prevent.
    # A real cost from a real seat or side call is the only evidence, so an
    # unevidenced total is dropped rather than printed.
    reported = any_cost
    return {"total_usd": total if reported else None,
            "by_seat": rows, "by_kind": side,
            # `reported` is the difference between "this run was free" and
            # "no CLI in this room reports cost". Without it a card with an
            # empty spend block states the first and means the second.
            "reported": reported}


def _gate(meta, entries, trimmed):
    """Per-wave verification history — the only place it exists.

    ``continuous.gate.last`` keeps just the newest result; the per-wave record
    is these trace entries. They arrive in pairs: a ``running`` entry when the
    command starts and a ``passed``/``failed`` one when it answers, so a run
    that died mid-gate leaves an unmatched ``running`` — counted separately
    rather than quietly dropped, because "the gate never came back" is a
    different and more alarming fact than "the gate failed".
    """
    cfg = _d(_d(meta.get("continuous")).get("gate"))
    last = _d(cfg.get("last"))
    passed = failed = started = 0
    last_fail, commits = None, []
    for row in entries:
        if _s(row.get("type")) != TYPE_GATE:
            continue
        status = _s(row.get("status"))
        sha = _s(row.get("commit"), 40)
        if sha and sha not in commits:
            commits.append(sha)
        if status == "passed":
            passed += 1
        elif status == "failed":
            failed += 1
            # The TAIL, not the head. `wave_gate` already tail-slices the
            # command's output into `detail`, so slicing the front off that
            # keeps the middle and throws the assertion away — the one line
            # the reader opened the card for. Measured: a 400-line pytest
            # tail ending "AssertionError: THE POINT" came back ending
            # "line 166". The cut announces itself, like every other here.
            text = _s(row.get("detail"))
            last_fail = (("[earlier output not kept]\n" + text[-TAIL_MAX:])
                         if len(text) > TAIL_MAX else text)
        elif status == "running":
            started += 1
        # `committed` is the commit-binding entry and is deliberately not an
        # outcome — see the note beside the TYPE_ table.
    unfinished = max(0, started - passed - failed)
    command = _s(cfg.get("command"))
    skipped = _s(last.get("skipped"))
    # The verification gate exists ONLY in Keep Improving: `wave_gate`
    # returns None immediately unless `continuous_on(state)`. So a Build
    # Together room — supervised, with a full control log — never had a
    # verification stage at all, and telling its reader "no verification
    # command was configured" names a setting they forgot to fill in for a
    # field that room does not have. Keying on the manager was wrong for
    # exactly the rooms most likely to be read here. The row is dropped and
    # the notes carry the sentence — a withholding is still STATED, just
    # once and in the right words.
    applicable = bool(_d(meta.get("continuous")).get("on")) or bool(cfg)
    return {
        "applicable": applicable,
        "command": command or None,
        "passed": passed, "failed": failed,
        # A gate that started and never reported. Only meaningful when the
        # record is whole: on a trimmed trace an early `running` entry can
        # simply have outlived the `passed` that answered it.
        "unfinished": unfinished if not trimmed else 0,
        "floor": trimmed,
        # Checkpoint shas, from the trace FIELD wave_gate stamps. Chats that
        # ran before that field existed carry the sha only inside the gate
        # entry's prose, and this deliberately does not go looking for it
        # there: an empty list means "none recorded", which the card words as
        # such rather than as "nothing was committed".
        "commits": commits,
        "last_failure": last_fail or None,
        "last_ok": last.get("ok") if isinstance(last.get("ok"), bool) else None,
        "last_seconds": last.get("seconds")
        if isinstance(last.get("seconds"), (int, float))
        and not isinstance(last.get("seconds"), bool) else None,
        # Stated, not omitted: no configured command is a CHOICE, and a card
        # with no verification row reads as "verification was not mentioned".
        "skipped": (skipped or (None if command else
                                "no verification command was configured"))
        if applicable else None,
    }


def _objectives(meta):
    """Settled objectives, from the archive no surface has ever read.

    ``archive_objective`` writes one record per objective the manager closed
    — goal, task count, failures, the files verified on disk, and that
    objective's last gate. It is the only durable account of an overnight
    run's shape, and until now it existed purely to be summarised into a
    memory note.
    """
    pol = _d(meta.get("continuous"))
    history = _l(pol.get("history"))
    settled, met, unknown = [], 0, 0
    for record in history[-OBJECTIVES_MAX:]:
        row = _d(record)
        gate = _d(row.get("gate"))
        settled.append({
            "goal": " ".join(_s(row.get("goal")).split())[:GOAL_MAX],
            "tasks": _n(row.get("tasks")),
            "failed": _n(row.get("failed")),
            "delivered": [_s(f) for f in _l(row.get("delivered"))
                          if _s(f)][:DELIVERED_MAX],
            "gate_ok": gate.get("ok") if isinstance(gate.get("ok"), bool)
            else None,
            # "met" | "abandoned" | "" — see relay.archive_objective. A
            # record written before the engine distinguished them is
            # honestly unknown and is NOT counted as met.
            "outcome": _s(row.get("outcome")),
        })
    for record in history:
        kind = _s(_d(record).get("outcome"))
        if kind == "met":
            met += 1
        elif kind != "abandoned":
            unknown += 1
    names = [" ".join(_s(g).split())[:GOAL_MAX] for g in _l(pol.get("objectives"))]
    return {"settled": settled,
            # Counted over the WHOLE archive, so the card's figure is not the
            # length of the slice it happens to be showing — the earlier
            # version chipped "12 objectives met" for a run that settled 30.
            "recorded": len(history),
            "met": met, "unknown": unknown,
            # ...and the slice announces itself, like every other cut here.
            "shown": len(settled), "listed_all": len(settled) == len(history),
            "attempted": [n for n in names if n]}


def _tasks(meta):
    """The board as it stands. Open work is what a resumed run picks up."""
    counts = {"done": 0, "failed": 0, "open": 0, "total": 0}
    delivered = []
    for task in _l(meta.get("workstreams")):
        row = _d(task)
        counts["total"] += 1
        status = _s(row.get("status"))
        if status == "done":
            counts["done"] += 1
        elif status == "failed":
            counts["failed"] += 1
        else:
            counts["open"] += 1
        for path in _l(_d(row.get("verified")).get("delivered")):
            if _s(path):
                delivered.append(_s(path))
    return counts, delivered


def _delivered(meta):
    """Every file this run verified on disk, deduped, newest objective last.

    Both halves are needed and neither is a superset: the live board holds
    only the CURRENT objective's tasks (``archive_objective`` sets
    ``workstreams`` to None on rollover), and the archive holds only settled
    ones. Order is preserved so the newest work reads last, which is where a
    reader looks first after a rollover.
    """
    seen, out = set(), []
    for record in _l(_d(meta.get("continuous")).get("history")):
        for path in _l(_d(record).get("delivered")):
            name = _s(path)
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    _, live = _tasks(meta)
    for name in live:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _clock(meta, pol, entries, trimmed):
    """Times, and exactly ONE of them may be called a duration.

    **Only ``ran_for_s`` is a duration.** It is Keep Improving's accumulated
    run seconds, added up at each barrier, and it is the one number here that
    means "time actually spent working". Everything else is a SPAN between
    two instants, and a span is not a duration whenever the thing being
    measured stopped and started again — which is the normal case here.

    The two spans are published because a range is genuinely useful, and both
    are labelled by what they bracket rather than by how long anything ran:

    ``open_for_s`` is created→updated: how long the chat has EXISTED. One
    real chat reports 72,949s (20 hours) for a session worked on, closed and
    reopened the next day.

    ``active_from``/``active_to`` bracket the control log's own first and
    last entry. An earlier version turned that bracket into ``active_s`` and
    promoted it to the printable duration, which was the same mistake one
    field over and measurably worse: on the real chat
    ``20260822-220535`` the log spans 22:09 to 09:04 the next morning across
    ELEVEN entries, so the card read "10h 55m active" for a run that worked
    for minutes. It is also trace-derived, so it SHRINKS as the log is
    trimmed — an eight-hour run whose surviving entries start at 07:40 would
    have claimed "1h 20m". ``active_floor`` says when the bracket lost its
    beginning; the range is still true of what the record holds, and the card
    words it as "recorded activity", never as how long anything took.
    """
    start, end = _when(meta.get("created")), _when(meta.get("updated"))
    span = None
    if start and end and end >= start:
        span = int((end - start).total_seconds())
    ran = pol.get("elapsed_s")
    ran = int(ran) if isinstance(ran, (int, float)) \
        and not isinstance(ran, bool) and ran >= 0 else None
    stamps = [t for t in (_when(_s(r.get("ts"))) for r in entries) if t]
    return {"from": _s(meta.get("created")), "to": _s(meta.get("updated")),
            "open_for_s": span, "ran_for_s": ran,
            "active_from": _s(stamps and min(stamps).isoformat(" ", "seconds")),
            "active_to": _s(stamps and max(stamps).isoformat(" ", "seconds")),
            "active_floor": bool(stamps) and trimmed,
            # THE one printable duration, or None. Named so the renderer
            # never has to choose, and so there is exactly one place to look
            # when asking what the card is allowed to call a duration.
            "worked_s": ran}


def _provenance(meta):
    """Who decided this should run — a FACT when the engine recorded one.

    Absent on every chat saved before the field existed, and on those the
    card falls back to a duration threshold, which is a guess and is labelled
    as one rather than dressed up as provenance.
    """
    row = _d(meta.get("started_by"))
    kind = _s(row.get("kind"))
    if kind not in KINDS:
        return {"kind": None, "name": "", "when": "", "unattended": False,
                "manual": False, "known": False}
    return {"kind": kind, "name": _s(row.get("name"), 120),
            "when": _s(row.get("when"), 120),
            # A terminal run declares this with `--unattended`; nothing else
            # sets it, and its absence is not a claim that anyone was there.
            "unattended": row.get("unattended") is True,
            # A schedule fired by the Run-now button is NOT unattended: Josh
            # pressed it. Without this the card headed a run he started
            # thirty seconds earlier "While you were away" and told him a
            # timer had done it at 01:00.
            "manual": row.get("manual") is True,
            "known": True}


# Starts that are unattended BY CONSTRUCTION: nobody asked for them at the
# moment they happened. A terminal run is not in the list because it is
# usually Josh at a console — it qualifies only when `--unattended` says so.
AWAY_KINDS = ("schedule", "webhook")

# The provenance vocabulary, and a DELIBERATE second copy of
# `relay.STARTED_BY_KINDS` — this module imports nothing from relay, which is
# what makes it testable without the engine. The copy is the same trade
# `browser_mcp._confine` makes against `relay.confine_to_workspace`, and it
# is kept honest the same way: by a parity test, not by this comment. Drift
# here fails OPEN in the harmless direction (an unrecognised kind reads as
# "not recorded"), but it would silently stop a new front end's runs from
# ever opening with a card.
KINDS = ("josh", "schedule", "webhook", "terminal")


def build(meta, facts=None, now=None):
    """One report for one chat. Never raises; never invents.

    ``facts`` carries what relay has already worked out — ``status``
    (``supervisor_status``), ``interrupted`` (``was_interrupted``),
    ``running`` (the live run registry) and ``trace_cap``
    (``SUPERVISOR_TRACE_MAX``). Absent facts degrade to "not stated" rather
    than to a re-derivation.
    """
    try:
        return _build(_d(meta), _d(facts), now)
    except Exception as exc:                      # never break a reopen
        return {"ok": False, "show": False,
                "reason": "This chat's record could not be read (%s)."
                          % type(exc).__name__}


def _build(meta, facts, now):
    entries = [r for r in _l(meta.get("supervisor_trace")) if isinstance(r, dict)]
    pol = _d(meta.get("continuous"))
    trimmed = _trimmed(entries, facts.get("trace_cap"))
    counts = {"waves": 0, "errors": 0, "revivals": 0}
    limits, verdicts = [], []
    for row in entries:
        kind = _s(row.get("type"))
        if kind == TYPE_PLAN:
            counts["waves"] += 1
        elif kind == TYPE_ERROR:
            counts["errors"] += 1
        elif kind == TYPE_REVIVED:
            counts["revivals"] += 1
        elif kind == TYPE_LIMIT:
            limits.append(_s(row.get("detail"), 400) or _s(row.get("title"), 400))
        elif kind in (TYPE_ACCEPTED, TYPE_UNRESOLVED):
            verdicts.append({"type": kind, "title": _s(row.get("title"), 240),
                             "detail": _s(row.get("detail"), 600)})

    task_counts, _ = _tasks(meta)
    objectives = _objectives(meta)
    completion = _d(meta.get("completion"))
    # The manager's own goal, falling back to the opener ONLY with the
    # fallback named. They are different things: one is what the run decided
    # to pursue, the other is what Josh typed, and a card that prints the
    # opener under the word "objective" is the Master-goal-panel bug again.
    goal = " ".join(_s(meta.get("supervisor_goal")).split())[:GOAL_MAX]
    source = "supervisor"
    if not goal:
        goal, source = " ".join(_s(meta.get("topic")).split())[:GOAL_MAX], "opener"

    prov = _provenance(meta)
    clock = _clock(meta, pol, entries, trimmed)
    files = _delivered(meta)
    report = {
        "ok": True,
        "id": _s(meta.get("id")),
        "title": _s(meta.get("title"), 200),
        "mode": _s(meta.get("mode")),
        "continuous": bool(pol.get("on")),
        "running": bool(facts.get("running")),
        "interrupted": bool(facts.get("interrupted")),
        # relay.supervisor_status' dict, passed straight through. It is a
        # dict, not a word — an early version stringified it and every card
        # silently rendered an empty status.
        "status": _d(facts.get("status")) or None,
        "started_by": prov,
        "clock": clock,
        "objective": {"text": goal, "source": source if goal else ""},
        "outcome": {
            "lifecycle": _s(completion.get("lifecycle")) or None,
            # A mechanical stop and a goal verdict answer different
            # questions and the existing badges keep them apart on purpose.
            # Merging them into one headline is the goal_accepted /
            # goal_unresolved conflation one surface over.
            "stopped": _s(completion.get("termination_reason")) or None,
            "verdict": _s(completion.get("goal_verdict")) or None,
            "trace": verdicts[-1] if verdicts else None,
        },
        # `applicable` for the same reason the gate has one: a room with no
        # manager has no waves, and a chip reading "0 waves" is a count of a
        # concept that does not apply — sitting a few pixels above a note
        # saying the room has no waves to report.
        "waves": {"dispatched": counts["waves"], "floor": trimmed,
                  "applicable": _s(meta.get("mode")) == "supervisor"
                  or bool(entries)},
        "trouble": {"errors": counts["errors"], "revivals": counts["revivals"],
                    "limits": limits[-3:], "floor": trimmed},
        "gate": _gate(meta, entries, trimmed),
        "objectives": objectives,
        "tasks": task_counts,
        "delivered": files[:DELIVERED_MAX],
        "delivered_total": len(files),
        "spend": _spend(meta),
        "waiting": _d(meta.get("ask_pending")) or None,
        "trimmed": trimmed,
        "trace_cap": _n(facts.get("trace_cap")),
        "notes": [],
    }
    # `available` (is there anything here to report on) and `show` (does the
    # chat OPEN with the card) are different questions and were one for a
    # while, which meant a chat we could describe perfectly well was
    # unreachable because we could not prove nobody was watching it. The
    # button follows `available`; the card auto-opens on `show`; and the
    # refusal sentence is what the button paints when the two disagree.
    report["available"] = bool(entries or report["continuous"]
                               or report["spend"]["reported"]
                               or report["objectives"]["recorded"]
                               or prov["known"])
    report["notes"] = _notes(report, meta, entries)
    report["show"], report["reason"] = _show(report, entries)
    return report


def _notes(report, meta, entries):
    """The withholdings, as sentences. An omitted row is a claim; this is the
    module refusing to make it."""
    out = []
    if report["trimmed"]:
        # The CAP, never len(entries). Those coincide only on the path where
        # the cap itself is what detected the trim; on the structural path
        # (a log whose first row is not the run's first plan) they do not,
        # and an earlier version told the reader Alloy keeps three
        # control-log entries — a false statement about the product, made
        # inside the sentence whose whole job is to be trustworthy.
        cap = _n(report.get("trace_cap"))
        out.append(("Only the most recent %d control-log entries are kept, so "
                    % cap if cap else
                    "The start of the control log is no longer kept, so ")
                   + "the counts below are floors — the run did at least "
                     "this much.")
    if not report["spend"]["reported"]:
        out.append("No seat in this room reported a cost, so there is no "
                   "spend to show — not a spend of zero.")
    if report["gate"]["skipped"]:
        out.append("Verification: " + report["gate"]["skipped"] + ".")
    if report["objectives"]["recorded"] and not report["objectives"]["listed_all"]:
        out.append("%d objectives are recorded; the %d most recent are listed."
                   % (report["objectives"]["recorded"],
                      report["objectives"]["shown"]))
    if report["objectives"]["unknown"]:
        out.append("%d of them were recorded before Alloy distinguished an "
                   "objective the manager finished from one the check-in gave "
                   "up on, so they are counted as neither."
                   % report["objectives"]["unknown"])
    if not report["started_by"]["known"]:
        # Only claim the inference that was actually available. The note used
        # to say the decision came from a duration on chats where no duration
        # was ever measured — describing a method that was not used.
        out.append("This chat predates Alloy recording what started it, so "
                   + ("whether anyone was watching is inferred from how long "
                      "it worked, not known."
                      if report["clock"]["worked_s"] is not None else
                      "whether anyone was watching is not known, and no "
                      "working time was measured to infer it from."))
    if _s(meta.get("mode")) != "supervisor" and not entries:
        out.append("This room had no manager, so there are no waves, "
                   "objectives or verification to report.")
    # Said once, here, rather than left as a missing column: the token
    # figures on disk are not all trustworthy and stats.py is the reader
    # that knows which. See the module docstring.
    out.append("Token counts are not shown here — Stats is the one reader "
               "that handles the per-provider conventions correctly.")
    return out


def _show(report, entries):
    """Does this chat open with the card, and if not, why not.

    Provenance is consulted FIRST and settles it as a FACT. The order is
    load-bearing and was got wrong once: with the "is there anything to
    report" test on top, a scheduled room of any recipe but Supervisor —
    a nightly round-robin, a Talk Live room, a solo seat on a cron —
    produced no card at all, which is the paradigm case of this feature
    refusing to fire. A schedule, the webhook and ``--unattended`` all mean
    nobody was there, whatever the room was doing, and a thin card that
    names the duration, the ending and the spend still answers the question
    Josh is opening the chat to ask.

    Everything below provenance is a duration threshold, which is a guess —
    applied only where there is genuinely nothing better, and ``_notes``
    says out loud that it was a guess.
    """
    if not report["available"]:
        return False, "There is nothing recorded about this chat to report."
    prov = report["started_by"]
    if (prov["kind"] in AWAY_KINDS and not prov["manual"]) or prov["unattended"]:
        return True, ""
    # `was_interrupted` is TRUE FOR EVERY LIVE RUN: its evidence is
    # `lifecycle == "active"` with no termination reason, which is exactly
    # what a healthy run in progress looks like — the distinction it was
    # written for only exists once the process is gone. Without the
    # `running` half, pressing Send and switching tabs and back popped
    # "While you were away" on a forty-second-old conversation Josh had not
    # left, and did it again on every switch.
    if report["interrupted"] and not report["running"]:
        return True, ""
    if report["waiting"] and not report["running"]:
        return True, ""
    # The threshold is applied to how long the run was WORKING, and that is
    # the ONLY honest duration here: a chat's created→updated span measures
    # the chat, and the control log's first→last span includes every idle
    # gap between them (one real chat: eleven entries spread over eleven
    # hours). Neither says whether anyone was watching.
    span = report["clock"]["worked_s"]
    if span is not None and span >= AWAY_MIN_S:
        return True, ""
    if span is not None:
        return False, ("This chat worked for %s, so it is not one you walked "
                       "away from." % (human_duration(span) or "under a minute"))
    return False, ("Nothing here records this running without you — no "
                   "schedule, no crash, and no measured working time.")
