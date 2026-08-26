"""Transparent outcome aggregation and the human-editable Alloy playbook.

CONTRACT — read this before changing anything:

* Input is ONLY ``sessions/*/outcome.json`` records, exactly as
  ``outcome.read_outcome`` returns them. This module never opens a transcript,
  never shells out, and never imports relay beyond the outcome reader, so
  aggregation stays deterministic, token-free, and testable without the engine.
* Rules are DERIVED, never invented here: every heuristic traces to concrete
  session ids in its ``provenance``, and ``evidence_count`` is the number of
  DISTINCT sessions behind it. Two provenance kinds exist and every fresh rule
  labels itself explicitly in ``source``:
    - ``human_reason`` — Josh tagged a failure reason himself. One sighting is
      evidence; his word does not need to recur.
    - ``inferred_pattern`` — a structural pattern (repeated caps, unanswered
      asks) that must RECURRENCE before this module calls it a rule.
  ``source_reasons`` carries the triggering reason tags (human rules only);
  ``provenance_display`` is a ready-to-render sentence. ``provenance_label``
  answers the same question for ANY heuristic dict, including pre-``source``
  rules kept alive by a merge — readers never special-case old shapes.
  FOR CONSUMERS (the app, the UI, any future report): prefer the derived
  display fields over re-deriving your own — ``source`` /
  ``source_reasons`` / ``provenance_display`` on each heuristic answer "where
   did this rule come from", and ``summarize(records, playbook)`` returns the
  headline stats as structured data (``sessions_counted``, per-rating
  ``ratings``, ``rules`` tallies, and trimmed ``top_rules``, pinned rules
  first then ranked by evidence) so a caller never re-counts records or
  re-sorts heuristics. ``format_retro_report`` renders the same stats as
  text ("Top rules:" line); use it when text is enough.
* Headline stats: consumers wanting NUMBERS call ``summarize(records,
  playbook)`` — one aggregation pass rendered as ``sessions_counted``,
  ``ratings`` (helpful/not_helpful/skipped/unrated), ``rules`` counts
  (total/active/pinned/dismissed), and ``top_rules`` trimmed to display
  fields (heuristic_id, directive, evidence_count, source, pinned,
  provenance_display). Never parse ``format_retro_report``'s text for
  these; the report is for humans, the dict is the interface. (Spend is
  different data entirely: per-kind usage lives in outcome records under
  ``hard_facts.usage.by_kind`` / ``by_seat`` — see outcome.py, not here.)
* Josh's editorial decisions outrank the derivation: across every refresh a
  dismissed rule stays dismissed and a pinned rule keeps its wording. Unpinned
  rules the derivation stops producing decay after TTL_DAYS.
* Shapes are ADDITIVE-ONLY: existing keys never change meaning or move, and
  outside readers (relay.playbook_block, the app) may depend on any key
  documented here. ``run_retro``'s ``(playbook, report, path)`` return is part
  of that contract.
"""

import datetime
import json
import os

import outcome

PLAYBOOK_FILE = "playbook.json"
PLAYBOOK_VERSION = 1
TTL_DAYS = 30

# Where a rule's authority comes from. See the module CONTRACT above for why
# exactly these two exist and why they are treated differently.
SOURCE_HUMAN_REASON = "human_reason"
SOURCE_INFERRED_PATTERN = "inferred_pattern"
PROVENANCE_SOURCES = (SOURCE_HUMAN_REASON, SOURCE_INFERRED_PATTERN)

# How many rules summarize() lifts out as "top" — enough for a headline,
# few enough to stay one.
SUMMARY_TOP_RULES_MAX = 3

REASON_RULES = {
    "incorrect": ("verify-results", "Assign an independent verifier before accepting factual or code conclusions."),
    "incomplete": ("raise-completion-budget", "Increase the round/turn budget or split the goal into smaller dependent tasks."),
    "inefficient": ("prefer-workstreams", "Use Supervisor workstreams for independent tasks instead of sequential turns."),
    "poor_coordination": ("explicit-ownership", "Give every task one owner, explicit deliverables, and only necessary dependencies."),
}


def scan_outcomes(sessions_dir):
    rows = []
    try:
        names = sorted(os.listdir(sessions_dir))
    except OSError:
        return rows
    for name in names:
        path = os.path.join(sessions_dir, name)
        if os.path.isdir(path):
            rec = outcome.read_outcome(path)
            if rec and rec.get("outcome_version") == outcome.OUTCOME_VERSION:
                rows.append(rec)
    return rows


def _expiry(now):
    return (now + datetime.timedelta(days=TTL_DAYS)).isoformat(timespec="seconds")


def provenance_label(rule):
    """One human sentence answering 'where did THIS rule come from?'.

    Accepts any heuristic dict — freshly derived, merged from the playbook,
    or a pre-``source``-era rule — so the report and any UI render old and
    new rules with one code path instead of special-casing shapes.
    """
    n = int(rule.get("evidence_count") or 0)
    sessions = "session" if n == 1 else "sessions"
    tags = [t for t in (rule.get("source_reasons") or []) if t]
    if rule.get("source") == SOURCE_HUMAN_REASON:
        if tags:
            return "Josh tagged '%s' in %d %s" % ("' and '".join(tags), n, sessions)
        return "from a reason Josh tagged in %d %s" % (n, sessions)
    if rule.get("source") == SOURCE_INFERRED_PATTERN:
        return "recurring structural pattern seen in %d %s" % (n, sessions)
    return "seen in %d %s" % (n, sessions)


def derive_heuristics(records, now=None):
    """Derive deterministic candidates; no model judgement enters here.

    Every candidate carries its provenance explicitly: ``source`` says whether
    the rule came from a reason Josh tagged himself (one sighting is evidence)
    or an inferred structural pattern (which must recur), ``source_reasons``
    names the triggering tags, and ``provenance_display`` is the rendered
    sentence. Existing keys keep their historical meaning.
    """
    now = now or datetime.datetime.now()
    evidence = {rid: [] for rid, _ in REASON_RULES.values()}
    reasons_hit = {rid: set() for rid in evidence}
    cap_ids, ask_ids = [], []
    for rec in records:
        sid = (rec.get("session") or {}).get("id")
        if not sid:
            continue
        feedback = rec.get("human_feedback") or {}
        if feedback.get("rating") == "not_helpful":
            for reason in feedback.get("reasons") or []:
                if reason in REASON_RULES:
                    rid = REASON_RULES[reason][0]
                    evidence[rid].append(sid)
                    reasons_hit[rid].add(reason)
        hard = rec.get("hard_facts") or {}
        if hard.get("ended") in ("cap", "ceiling"):
            cap_ids.append(sid)
        if (hard.get("asks") or {}).get("unanswered", 0):
            ask_ids.append(sid)

    candidates = []
    directives = {rid: text for rid, text in REASON_RULES.values()}
    for rid, ids in evidence.items():  # one explicit human reason is evidence
        if ids:
            candidates.append((rid, directives[rid], ids,
                               SOURCE_HUMAN_REASON, sorted(reasons_hit[rid])))
    # Inferred hard patterns must recur; one cap or missed ask is not a trend.
    if len(cap_ids) >= 2:
        candidates.append(("avoid-repeated-cap", "When similar runs repeatedly hit their cap, decompose the goal or raise the safety budget before retrying.", cap_ids,
                           SOURCE_INFERRED_PATTERN, []))
    if len(ask_ids) >= 2:
        candidates.append(("resolve-asks-early", "Ask essential human questions during planning, before parallel work begins.", ask_ids,
                           SOURCE_INFERRED_PATTERN, []))
    out = []
    for rid, directive, ids, source, tags in candidates:
        uniq = list(dict.fromkeys(ids))
        rule = {"heuristic_id": rid, "directive": directive,
                "provenance": uniq, "evidence_count": len(uniq),
                "expiry": _expiry(now), "status": "active", "pinned": False}
        rule["source"] = source
        rule["source_reasons"] = list(tags)
        rule["provenance_display"] = provenance_label(rule)
        out.append(rule)
    return out


def read_playbook(sessions_dir):
    try:
        with open(os.path.join(sessions_dir, PLAYBOOK_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def merge_heuristics(existing, candidates, now=None):
    """Refresh observed rules while preserving human pin/dismiss decisions."""
    now = now or datetime.datetime.now()
    old = {h.get("heuristic_id"): h for h in (existing or {}).get("heuristics", [])
           if isinstance(h, dict) and h.get("heuristic_id")}
    merged, seen = [], set()
    for candidate in candidates:
        rid = candidate["heuristic_id"]
        prior = old.get(rid) or {}
        if prior.get("status") == "dismissed":
            candidate["status"] = "dismissed"
        if prior.get("pinned"):
            candidate["pinned"] = True
            candidate["directive"] = prior.get("directive") or candidate["directive"]
        merged.append(candidate); seen.add(rid)
    for rid, prior in old.items():
        if rid in seen:
            continue
        if prior.get("status") == "dismissed" or prior.get("pinned"):
            merged.append(prior); continue
        try:
            expires = datetime.datetime.fromisoformat(prior.get("expiry") or "")
        except ValueError:
            expires = now
        if expires > now:
            merged.append(prior)
    return {"playbook_version": PLAYBOOK_VERSION,
            "updated": now.isoformat(timespec="seconds"),
            "heuristics": merged}


def write_playbook(sessions_dir, playbook):
    os.makedirs(sessions_dir, exist_ok=True)
    path = os.path.join(sessions_dir, PLAYBOOK_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(playbook, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def summarize(records, playbook):
    """Derived headline stats over one aggregation pass — pure, no writes.

    Returns::

        {"sessions_counted": int,          # outcome records actually read
         "ratings": {rating: count},       # incl. "unrated"
         "rules": {"total"|"active"|"pinned"|"dismissed": count},
         "top_rules": [trimmed rule dicts]}  # pinned first, then evidence

    Top rules are trimmed to the fields a reader should display (id, wording,
    evidence, provenance kind) — never the full ``provenance`` id list, which
    is session bookkeeping, not headline material.
    """
    ratings = {"helpful": 0, "not_helpful": 0, "skipped": 0, "unrated": 0}
    for rec in records:
        rating = (rec.get("human_feedback") or {}).get("rating") or "unrated"
        ratings[rating] = ratings.get(rating, 0) + 1
    heuristics = [h for h in (playbook or {}).get("heuristics", [])
                  if isinstance(h, dict)]
    active = [h for h in heuristics if h.get("status") == "active"]
    ranked = sorted(active,
                    key=lambda h: (not h.get("pinned"),
                                   -int(h.get("evidence_count") or 0)))
    top_rules = [{"heuristic_id": h.get("heuristic_id"),
                  "directive": h.get("directive"),
                  "evidence_count": int(h.get("evidence_count") or 0),
                  "source": h.get("source"),
                  "pinned": bool(h.get("pinned")),
                  "provenance_display": h.get("provenance_display")
                  or provenance_label(h)}
                 for h in ranked[:SUMMARY_TOP_RULES_MAX]]
    return {"sessions_counted": len(records),
            "ratings": ratings,
            "rules": {"total": len(heuristics),
                      "active": len(active),
                      "pinned": sum(1 for h in heuristics if h.get("pinned")),
                      "dismissed": sum(1 for h in heuristics
                                       if h.get("status") == "dismissed")},
            "top_rules": top_rules}


def format_retro_report(records, playbook):
    stats = summarize(records, playbook)
    ratings = stats["ratings"]
    lines = [f"Retro: {len(records)} outcome(s) · {ratings['helpful']} helpful · "
             f"{ratings['not_helpful']} not helpful · {ratings['unrated']} unrated"]
    active = [h for h in playbook.get("heuristics", []) if h.get("status") == "active"]
    if not active:
        return lines[0] + "\nNo active heuristics yet."
    lines += ["", "| Evidence | Heuristic | Recommendation |", "|---:|---|---|"]
    for h in active:
        lines.append(f"| {h.get('evidence_count', 0)} | {h['heuristic_id']} | {h['directive']} |")
    if stats["top_rules"]:
        tops = ", ".join("%s (%d)" % (r["heuristic_id"], r["evidence_count"])
                         for r in stats["top_rules"])
        lines += ["", f"Top rules: {tops}"]
    return "\n".join(lines)


def run_retro(sessions_dir, now=None):
    """One full pass: scan → derive → merge → write, then report.

    Returns ``(playbook, report_text, playbook_path)`` — the tuple shape
    callers (relay's /retro) depend on. ``summarize`` exposes the same pass
    as structured stats without re-reading anything.
    """
    records = scan_outcomes(sessions_dir)
    playbook = merge_heuristics(read_playbook(sessions_dir),
                                derive_heuristics(records, now=now), now=now)
    path = write_playbook(sessions_dir, playbook)
    return playbook, format_retro_report(records, playbook), path
