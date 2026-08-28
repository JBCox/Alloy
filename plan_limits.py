"""Anthropic PLAN quota — the number Alloy was already being handed and threw away.

Standalone like export/fork/stats/memory/schedule: stdlib only, imports nothing
from relay or app, no public function raises, and it owns NO root directory and
NO module-level store. relay captures the readings off the stream and app holds
the snapshot; this module only parses, merges, words and DECIDES.

Measured 2026-08-28 against a real `claude -p --output-format stream-json` turn
(claude 2.x, Max plan). Every turn emits ONE top-level line Alloy had never
looked at -- `grep -rn "rate_limit"` over the whole repo returned nothing:

    {"type": "rate_limit_event", "session_id": ..., "uuid": ...,
     "rate_limit_info": {"status": "allowed_warning", "resetsAt": 1788044400,
                         "rateLimitType": "seven_day", "utilization": 0.79,
                         "isUsingOverage": false, "surpassedThreshold": 0.75}}

`utilization: 0.79` and `resetsAt -> Sat 29 Aug 6:00 PM` reproduce the desktop
app's "Weekly - all models / Resets Sat 6:00 PM / 79%" row exactly, to the
minute. Confirmed identical on a second turn, so it is per-turn, not once.

Five rules carry this module.

**Absence is not zero.** A limit nobody reported is None, never 0.0 -- the
`stats.py` rule, and here it is load-bearing rather than cosmetic, because a
brake reading a manufactured 0% allows every run forever.

**The feed is THRESHOLD-GATED, and that bounds what a brake can promise.**
`surpassedThreshold: 0.75` is in the payload, and in the measured run the
`seven_day` limit at 79% emitted an event while the `five_hour` limit -- 1% at
that moment, per the account's own usage panel -- emitted nothing at all. So
silence means "under the reporting threshold", and a threshold set BELOW it can
never fire. `enforceable_floor` publishes that number so a front end can say so
instead of shipping a control that does nothing (this repo's most-repeated
defect). It is inferred from one account's observed payloads, never assumed: with
nothing observed the floor is None and the honest answer is "not known yet".

**A reading is a LOWER BOUND until its window resets.** Utilization only
accumulates inside a window, so an 18:00 reading of 79% still means ">= 79%" at
01:00 -- which makes REFUSING on a stale reading sound, and ALLOWING on one
unsound. `resetsAt` is therefore the expiry: past it the window is a new one and
the number means nothing. Both halves matter; dropping the second is how a brake
refuses a nightly job for a week over a quota that reset on Saturday.

**Merge, never replace.** A turn reports only the limit(s) that crossed a
threshold, so a fresh `seven_day` reading must not erase a `five_hour` one
observed an hour ago. Keyed by `rateLimitType`.

**Account-level, not session-level.** The same number for every Claude seat in
every open chat, so it never rides a message row and is never written into a
session's meta -- which would record it as a fact about that conversation. That
is the whole reason it is not modelled on `last_context`, which it otherwise
resembles.
"""

import time

# The vocabulary the CLI uses for each quota window, and what a human calls it.
# An unknown type is KEPT and titled from its own key rather than dropped: this
# list came from one account on one day, and a limit Alloy cannot name is still
# a limit it must be able to refuse on.
LIMIT_NAMES = {
    "five_hour": "5-hour limit",
    "seven_day": "Weekly limit",
    "seven_day_opus": "Weekly limit (Opus)",
    "seven_day_fable": "Weekly limit (Fable)",
}

# Statuses the payload's `status` field has been seen to carry. Only used for
# wording; the DECISION is made on utilization and isUsingOverage, never on a
# status string, because a vocabulary that drifts between CLI versions must not
# silently disable a brake.
OK_STATUSES = ("allowed", "allowed_warning")


def limit_title(kind):
    """Human name for a rateLimitType, including one we have never seen."""
    if not isinstance(kind, str) or not kind.strip():
        return "Plan limit"
    key = kind.strip()
    if key in LIMIT_NAMES:
        return LIMIT_NAMES[key]
    return key.replace("_", " ").strip().capitalize()


def parse_event(evt, now=None):
    """One `rate_limit_event` line -> a normalized reading, or None.

    Everything here arrives off a subprocess pipe, so every field is a CLAIM:
    a payload that is not a dict, a utilization that is not a number, a
    rateLimitType that is missing -- each returns None rather than raising into
    a seat's turn (the activity-hook contract: narration must never fail work).
    """
    try:
        if not isinstance(evt, dict):
            return None
        if evt.get("type") != "rate_limit_event":
            return None
        info = evt.get("rate_limit_info")
        if not isinstance(info, dict):
            return None
        kind = info.get("rateLimitType")
        if not isinstance(kind, str) or not kind.strip():
            return None
        util = info.get("utilization")
        # bool is an int subclass and would sail through a number check into
        # a utilization of 1.0; reject it explicitly.
        if isinstance(util, bool) or not isinstance(util, (int, float)):
            return None
        util = float(util)
        if util != util or util < 0:          # NaN, negative
            return None
        out = {
            "kind": kind.strip(),
            "title": limit_title(kind),
            "utilization": util,
            "observed_at": float(now if now is not None else time.time()),
        }
        resets = info.get("resetsAt")
        if not isinstance(resets, bool) and isinstance(resets, (int, float)) \
                and resets > 0:
            out["resets_at"] = float(resets)
        if info.get("isUsingOverage") is True:
            out["overage"] = True
        thr = info.get("surpassedThreshold")
        if not isinstance(thr, bool) and isinstance(thr, (int, float)) \
                and 0 < float(thr) <= 1:
            out["reported_above"] = float(thr)
        status = info.get("status")
        if isinstance(status, str) and status.strip():
            out["status"] = status.strip()
        return out
    except Exception:
        return None


def merge(snapshot, reading):
    """Fold one reading into an account snapshot. Returns a NEW dict.

    Merge rather than replace: a turn reports only the limits that crossed a
    threshold, so a `seven_day` reading arriving alone must leave an earlier
    `five_hour` one standing.
    """
    out = dict(snapshot) if isinstance(snapshot, dict) else {}
    if isinstance(reading, dict) and reading.get("kind"):
        out[reading["kind"]] = dict(reading)
    return out


def live_readings(snapshot, now=None):
    """The readings whose window has not yet reset, newest-window first.

    Past `resets_at` the quota window is a different one and the stored
    percentage describes a period that is over. A reading with no `resets_at`
    at all cannot be expired and is kept -- withholding it would be inventing
    an expiry the CLI never reported.
    """
    if not isinstance(snapshot, dict):
        return []
    t = float(now if now is not None else time.time())
    out = []
    for value in snapshot.values():
        if not isinstance(value, dict):
            continue
        resets = value.get("resets_at")
        if isinstance(resets, (int, float)) and not isinstance(resets, bool) \
                and t >= float(resets):
            continue
        out.append(value)
    out.sort(key=lambda r: (-float(r.get("utilization") or 0.0),
                            str(r.get("kind") or "")))
    return out


def worst(snapshot, now=None):
    """The most-consumed live limit, or None when nothing has been reported.

    None is a real answer and is NOT 0%: it means no measurement exists, which
    a caller must be able to tell apart from a measured zero.
    """
    live = live_readings(snapshot, now=now)
    return dict(live[0]) if live else None


def enforceable_floor(snapshot):
    """Lowest utilization the feed has actually been seen to report, or None.

    The CLI stays silent below its own warning threshold, so a brake set under
    this number cannot ever fire. Publishing it is what lets a front end say
    that out loud instead of offering a setting that quietly does nothing.
    None means no payload has carried a threshold yet, so nothing is known --
    deliberately not a hardcoded 0.75, which would state one account's observed
    behaviour as a property of the product.
    """
    if not isinstance(snapshot, dict):
        return None
    seen = [float(v["reported_above"]) for v in snapshot.values()
            if isinstance(v, dict)
            and isinstance(v.get("reported_above"), (int, float))
            and not isinstance(v.get("reported_above"), bool)]
    return min(seen) if seen else None


def pct(util):
    """A utilization as whole-percent text. Rounds toward the SCARY side.

    0.796 shown as 79% then refused by a brake set at 80% reads as a bug, so
    the displayed number never understates what the decision was made on.
    """
    if not isinstance(util, (int, float)) or isinstance(util, bool):
        return None
    import math
    return "%d%%" % int(math.ceil(float(util) * 100 - 1e-9))


def describe(reading, now=None):
    """One reading as a sentence. Empty string for nothing worth saying."""
    if not isinstance(reading, dict):
        return ""
    share = pct(reading.get("utilization"))
    if share is None:
        return ""
    bits = ["%s at %s" % (reading.get("title") or "Plan limit", share)]
    if reading.get("overage"):
        bits.append("using overage")
    resets = reading.get("resets_at")
    if isinstance(resets, (int, float)) and not isinstance(resets, bool):
        t = float(now if now is not None else time.time())
        left = float(resets) - t
        if left > 0:
            bits.append("resets %s" % _short_delay(left))
    return ", ".join(bits)


def _short_delay(seconds):
    """'in 3 hr 20 min' / 'in 2 days'. Never a bare timestamp: a reset time is
    only useful relative to now, and an absolute clock in a 01:00 log entry
    makes the reader do the arithmetic."""
    seconds = max(0.0, float(seconds))
    if seconds < 3600:
        return "in %d min" % max(1, int(seconds // 60))
    if seconds < 86400:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return "in %d hr %d min" % (hours, mins) if mins else "in %d hr" % hours
    days = seconds / 86400.0
    return "in 1 day" if days < 2 else "in %d days" % int(days)


def summary(snapshot, now=None):
    """Every live limit as one line, worst first. '' when nothing is known."""
    return " - ".join(s for s in (describe(r, now=now)
                                  for r in live_readings(snapshot, now=now))
                      if s)


def normalize_threshold(value):
    """A brake setting -> a fraction in (0, 1], or None for 'no brake'.

    Accepts a percentage (80) or a fraction (0.8), because the UI shows percent
    and the payload is a fraction, and a setting that means 80x quota if you
    typed the wrong one is not a setting. 0, None and junk all mean NO BRAKE:
    an unparseable number must never become an accidental limit, and it must
    never become 0.0 either, which would refuse every run forever (the
    `x or DEFAULT_CEILING` lesson, one field over -- test `is None`, never
    truthiness).
    """
    try:
        if value is None or isinstance(value, bool):
            return None
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num <= 0:
        return None
    if num > 1:
        num = num / 100.0
    if num <= 0 or num > 1:
        return None
    return num


def brake_verdict(snapshot, threshold, now=None):
    """May an UNATTENDED run start? -> (allow: bool, sentence: str).

    The sentence is never empty and always lands on the record, because nobody
    is watching at 01:00 and a run that silently did not happen is
    indistinguishable from one that did (`_launch_schedule`'s rule).

    It FAILS OPEN, deliberately, and that is a real trade this docstring will
    not dress up. With no measurement the run starts. The brake exists to stop
    a KNOWN overage, not to gate work on whether a probe succeeded, and a
    nightly job silently cancelled because a CLI call timed out is the worse of
    the two failures -- so an absent reading allows and SAYS the number is
    missing. Refusing on a stale-but-unexpired reading is the sound direction
    and is done without hesitation: inside one window utilization only climbs,
    so an old number is a lower bound (see the module docstring).
    """
    limit = normalize_threshold(threshold)
    if limit is None:
        return True, "No plan-limit brake is set."
    live = live_readings(snapshot, now=now)
    if not live:
        return True, ("No plan usage has been reported yet, so the %s brake "
                      "could not be checked -- starting anyway."
                      % (pct(limit) or "configured"))
    over = [r for r in live
            if r.get("overage")
            or float(r.get("utilization") or 0.0) >= limit]
    if over:
        worst_over = over[0]
        return False, ("Refused: %s, at or past the %s brake."
                       % (describe(worst_over, now=now), pct(limit)))
    return True, ("Plan usage under the %s brake (%s)."
                  % (pct(limit), summary(snapshot, now=now)))


def brake_note(snapshot, threshold):
    """The warning a front end shows BEFORE Josh arms an unattended run.

    Two things it must say and one it must not. It states the current reading
    (or that there is none), and it states when the setting cannot be enforced
    -- a threshold under the feed's own reporting floor is exactly the control
    that looks configured and does nothing. What it must not do is imply the
    brake makes an unattended run safe: it checks a quota, and it is checked
    once, at the moment the run starts.
    """
    limit = normalize_threshold(threshold)
    lines = []
    now = time.time()
    text = summary(snapshot, now=now)
    lines.append(("Plan usage now: %s." % text) if text
                 else "No plan usage reported yet -- Claude only reports a "
                      "limit once it passes its own warning threshold.")
    if limit is None:
        lines.append("No brake set: an unattended run starts whatever the "
                     "quota says.")
        return lines
    lines.append("An unattended run will be refused at or above %s of any "
                 "limit, checked once when it starts." % pct(limit))
    floor = enforceable_floor(snapshot)
    if floor is not None and limit < floor:
        lines.append("Note: this account has only ever reported a limit once "
                     "it passed %s, so a %s brake cannot fire -- nothing "
                     "below %s is visible to Alloy."
                     % (pct(floor), pct(limit), pct(floor)))
    return lines
