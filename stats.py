"""Cross-session statistics over ``sessions/*/outcome.json``.

Standalone on purpose, like ``export.py`` and ``fork.py``: stdlib plus
``outcome``'s reader, nothing from relay or app. It opens no transcript,
shells out to nothing, and never writes — so it is deterministic and
token-free to test.

CONTRACT

* Input is ONLY finished outcome records. A session with no ``outcome.json``
  simply is not counted; there is no partial-credit guess from a transcript.
* **Nothing here is estimated.** A number a CLI never reported is ``None``
  and renders as a blank, never as 0. That distinction is the whole point:
  Gemini and OpenCode report no cost at all, and a 0 in a spend column would
  read as "this seat was free" rather than "nobody said".
* Rows are grouped by **provider** and by **provider:model**, never by seat
  id — a seat id means a different agent in every chat, so summing "seat 0"
  across sessions would add up nothing at all.

THE CACHE CONVENTION IS PER PROVIDER, AND THAT IS MEASURED

The two CLIs that report cached tokens disagree about what the number means,
and using one formula for both is wrong by roughly 2x in one direction or
1000x in the other:

* **claude — DISJOINT.** ``input_tokens`` excludes everything served from or
  written to cache. Measured 2026-08-27: a turn reported ``input_tokens: 10``
  with ``cached_tokens: 86,646``, and the same turn's context, derived
  independently from its assistant events as
  ``input + cache_creation + cache_read``, came to **86,656** — exactly
  ``10 + 86,646``. Two separate measurements agreeing is what settles it.
  So the prompt is ``input + cached``.
* **codex — SUBSET.** ``cached_input_tokens`` is part of ``input_tokens``,
  the OpenAI shape. Measured on the same day: turn 2 of a two-turn job
  reported ``input_tokens: 33,886`` with ``cached_tokens: 27,136``. Under a
  disjoint reading that turn's prompt would be 61,022 tokens of which
  33,886 were *fresh* — for a second turn whose new material is one short
  reply. Under a subset reading it is a 33,886-token prompt with 6,750
  fresh, which is what a growing thread actually looks like. claude's own
  second turns reported ``input_tokens: 10`` and ``18``; codex's reported
  33,886. That difference in shape is the measurement.
* **Anyone else — UNKNOWN**, so no prompt size and no hit rate. Gemini and
  OpenCode report no tokens at all today.

A combined cross-provider hit rate is therefore never computed: it would
average two different quantities. ``totals`` carries raw sums only.
"""

import os

import outcome

# What `cached_tokens` means for each provider that reports it. See the
# module docstring — both entries are measured, and an absent provider is
# deliberately absent rather than defaulted, because a wrong convention
# produces a confident, plausible, wrong number.
CACHE_CONVENTION = {"claude": "disjoint", "gpt": "subset"}

# Fields summed straight across every seat row. `cost_usd` is NOT here: it
# stays None until some CLI reports one, so that "not reported" survives.
SUM_FIELDS = ("input_tokens", "output_tokens", "total_tokens",
              "cached_tokens", "wall_ms")

# The lowest `basis_version` at which a provider's TOKEN counters can be
# believed. Everything defaults to 1 — a basis-1 number simply means "taken
# at face value", which for claude was always correct.
#
# codex is the exception and the reason this table exists. Its counters are
# thread-CUMULATIVE and were being summed until 2026-08-27, so a real chat
# on disk holds 40,428,770 input tokens on ONE row and over half a BILLION
# summed. Those records are not rewritten (history never is), so a reader
# has to leave their token counters out and SAY it left them out — a
# footnote under a 559-million-token headline is not a correction, it is a
# caption on a lie.
MIN_TRUSTED_BASIS = {"gpt": 2}


def scan(sessions_dir):
    """Every readable outcome record under `sessions_dir`, newest last.

    Mirrors retro.scan_outcomes deliberately rather than importing it: this
    module must stay usable without retro, and the loop is four lines.
    """
    records = []
    try:
        names = sorted(os.listdir(sessions_dir))
    except OSError:
        return records
    for name in names:
        path = os.path.join(sessions_dir, name)
        if not os.path.isdir(path):
            continue
        rec = outcome.read_outcome(path)
        if isinstance(rec, dict):
            records.append(rec)
    return records


def prompt_tokens(provider, input_tokens, cached_tokens):
    """How many tokens were in the prompt, or None when we cannot say.

    None is a real answer here and must not be collapsed to a number: a
    provider whose convention is not in the measured table has counters we
    cannot combine, and a plausible wrong total is worse than a blank. It is
    also None when nothing was ever reported at all — `cached_tokens` of
    None means "no CLI said", which is not the same claim as zero.
    """
    convention = CACHE_CONVENTION.get(provider)
    if convention is None or input_tokens is None or cached_tokens is None:
        return None
    inp = int(input_tokens or 0)
    cached = int(cached_tokens or 0)
    if convention == "disjoint":
        return inp + cached
    return inp                      # subset: cached is already inside input


def cache_hit(provider, input_tokens, cached_tokens):
    """Share of the prompt that came from cache, or None.

    None when the provider's convention is unknown, when nothing was sent,
    or when the numbers contradict the convention (a subset provider
    reporting more cached than input). Clamped to 1.0 rather than reported
    above 100%: a rate over one is a broken assumption, not a discovery.
    """
    # `prompt_tokens` already answers None for an absent cached count, so
    # there is no separate guard for it here: a RED pass adding one changed
    # no behaviour, and the repo has been burned by comments that promote an
    # equivalent line to a rule (see the normcase note in CLAUDE.md).
    total = prompt_tokens(provider, input_tokens, cached_tokens)
    if not total or total <= 0:
        return None
    cached = int(cached_tokens or 0)
    if cached <= 0:
        return 0.0
    if cached > total:
        return None
    return round(cached / total, 4)


def trusted_basis(provider, basis_versions):
    """Can this seat's TOKEN counters be added up?

    `basis_versions` absent means 1 — every record written before the label
    existed. A mixed chat is judged by its OLDEST basis, because the summed
    total is only as good as the worst number in it.
    """
    floor = MIN_TRUSTED_BASIS.get(provider, 1)
    seen = [int(b) for b in (basis_versions or [1])
            if isinstance(b, (int, float))]
    return (min(seen) if seen else 1) >= floor


def _blank_row(key, label):
    row = {"key": key, "label": label, "sessions": 0, "turns": 0,
           "cost_usd": None, "cache_hit": None, "prompt_tokens": None,
           # sessions whose token counters were left out, and why
           "superseded_sessions": 0}
    for field in SUM_FIELDS:
        # None, not 0: "no CLI ever reported this" and "it reported zero"
        # are different answers and only one of them is a measurement
        row[field] = None
    return row


def _add(row, seat_usage, turns, tokens_ok):
    row["turns"] += int(turns or 0)
    cost = seat_usage.get("cost_usd")
    if isinstance(cost, (int, float)):
        row["cost_usd"] = round((row["cost_usd"] or 0.0) + float(cost), 6)
    if not tokens_ok:
        return
    for field in SUM_FIELDS:
        value = seat_usage.get(field)
        if value is not None:
            row[field] = int(row[field] or 0) + int(value)


def _finish(row, provider):
    row["prompt_tokens"] = prompt_tokens(provider, row["input_tokens"],
                                         row["cached_tokens"])
    row["cache_hit"] = cache_hit(provider, row["input_tokens"],
                                 row["cached_tokens"])
    return row


def collect(records):
    """Aggregate finished records into display-ready rows.

    Returns::

        {"sessions_counted": int,
         "sessions_with_usage": int,
         "totals": {turns, cost_usd|None, …SUM_FIELDS},
         "providers": [row, …],     # ranked by turns, then label
         "models":    [row, …]}     # provider:model, same shape

    Every row carries `cache_hit` (a 0-1 float or None) and `prompt_tokens`
    (an int or None); both are None wherever the provider's convention is
    not one this module has measured.
    """
    providers, models = {}, {}
    counted = with_usage = 0
    totals = _blank_row("all", "All seats")
    for rec in records:
        if not isinstance(rec, dict):
            continue
        counted += 1
        hard = rec.get("hard_facts")
        seats = (hard or {}).get("seats")
        if not isinstance(seats, list):
            continue
        touched = False
        seen_providers, seen_models = set(), set()
        # rows this record had at least one untrustworthy seat on. Counted
        # per ROW per RECORD, not per seat: two GPT seats in one chat are
        # one superseded session, not two.
        stale_rows, stale_here = set(), False
        for seat in seats:
            if not isinstance(seat, dict):
                continue
            provider = seat.get("provider") or "unknown"
            model = seat.get("model") or "—"
            usage = seat.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            turns = seat.get("turns") or 0
            if not usage and not turns:
                continue
            touched = True
            tokens_ok = trusted_basis(provider, usage.get("basis_versions"))
            prow = providers.setdefault(
                provider, _blank_row(provider, provider))
            mkey = "%s:%s" % (provider, model)
            mrow = models.setdefault(mkey, _blank_row(mkey, model))
            mrow["provider"] = provider
            _add(prow, usage, turns, tokens_ok)
            _add(mrow, usage, turns, tokens_ok)
            # a session counts ONCE per row however many seats it had on it
            if provider not in seen_providers:
                prow["sessions"] += 1
                seen_providers.add(provider)
            if mkey not in seen_models:
                mrow["sessions"] += 1
                seen_models.add(mkey)
            # totals sum the raw counters only; see the docstring on why
            # there is no combined hit rate
            _add(totals, usage, turns, tokens_ok)
            if not tokens_ok:
                stale_here = True
                for key, row in ((provider, prow), (mkey, mrow)):
                    if key not in stale_rows:
                        stale_rows.add(key)
                        row["superseded_sessions"] += 1
        if touched:
            with_usage += 1
            totals["sessions"] += 1
            if stale_here:
                totals["superseded_sessions"] += 1
    for key, row in providers.items():
        _finish(row, key)
    for row in models.values():
        _finish(row, row.get("provider"))
    rank = lambda r: (-int(r["turns"]), str(r["label"]).lower())
    return {"sessions_counted": counted,
            "sessions_with_usage": with_usage,
            "totals": totals,
            "providers": sorted(providers.values(), key=rank),
            "models": sorted(models.values(), key=rank)}


def gather(sessions_dir):
    """scan + collect in one call — what a front end wants."""
    return collect(scan(sessions_dir))
