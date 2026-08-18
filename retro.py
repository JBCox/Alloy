"""Transparent outcome aggregation and the human-editable Alloy playbook."""

import datetime
import json
import os

import outcome

PLAYBOOK_FILE = "playbook.json"
PLAYBOOK_VERSION = 1
TTL_DAYS = 30

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


def derive_heuristics(records, now=None):
    """Derive deterministic candidates; no model judgement enters here."""
    now = now or datetime.datetime.now()
    evidence = {rid: [] for rid, _ in REASON_RULES.values()}
    cap_ids, ask_ids = [], []
    for rec in records:
        sid = (rec.get("session") or {}).get("id")
        if not sid:
            continue
        feedback = rec.get("human_feedback") or {}
        if feedback.get("rating") == "not_helpful":
            for reason in feedback.get("reasons") or []:
                if reason in REASON_RULES:
                    evidence[REASON_RULES[reason][0]].append(sid)
        hard = rec.get("hard_facts") or {}
        if hard.get("ended") in ("cap", "ceiling"):
            cap_ids.append(sid)
        if (hard.get("asks") or {}).get("unanswered", 0):
            ask_ids.append(sid)

    candidates = []
    directives = {rid: text for rid, text in REASON_RULES.values()}
    for rid, ids in evidence.items():  # one explicit human reason is evidence
        if ids:
            candidates.append((rid, directives[rid], ids))
    # Inferred hard patterns must recur; one cap or missed ask is not a trend.
    if len(cap_ids) >= 2:
        candidates.append(("avoid-repeated-cap", "When similar runs repeatedly hit their cap, decompose the goal or raise the safety budget before retrying.", cap_ids))
    if len(ask_ids) >= 2:
        candidates.append(("resolve-asks-early", "Ask essential human questions during planning, before parallel work begins.", ask_ids))
    return [{"heuristic_id": rid, "directive": directive,
             "provenance": list(dict.fromkeys(ids)),
             "evidence_count": len(set(ids)), "expiry": _expiry(now),
             "status": "active", "pinned": False}
            for rid, directive, ids in candidates]


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


def format_retro_report(records, playbook):
    ratings = {"helpful": 0, "not_helpful": 0, "skipped": 0, "unrated": 0}
    for rec in records:
        rating = (rec.get("human_feedback") or {}).get("rating") or "unrated"
        ratings[rating] = ratings.get(rating, 0) + 1
    lines = [f"Retro: {len(records)} outcome(s) · {ratings['helpful']} helpful · "
             f"{ratings['not_helpful']} not helpful · {ratings['unrated']} unrated"]
    active = [h for h in playbook.get("heuristics", []) if h.get("status") == "active"]
    if not active:
        return lines[0] + "\nNo active heuristics yet."
    lines += ["", "| Evidence | Heuristic | Recommendation |", "|---:|---|---|"]
    for h in active:
        lines.append(f"| {h.get('evidence_count', 0)} | {h['heuristic_id']} | {h['directive']} |")
    return "\n".join(lines)


def run_retro(sessions_dir, now=None):
    records = scan_outcomes(sessions_dir)
    playbook = merge_heuristics(read_playbook(sessions_dir),
                                derive_heuristics(records, now=now), now=now)
    path = write_playbook(sessions_dir, playbook)
    return playbook, format_retro_report(records, playbook), path
