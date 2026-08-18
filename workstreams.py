"""Workstreams — different seats working on different things, concurrently.

The surprise in the engine is that most of this already exists. `compose_prompt`
builds each seat's prompt from ITS OWN queue (`state["pending"][i]`), and
`run_parallel` already runs one thread per seat at once. The reason parallel
mode feels like everyone doing the same thing is a single unconditional line in
`commit_reply`::

    for j, other in enumerate(agents):
        if other is not agent:
            state["pending"][j].append(...)

Every reply is fanned out to every seat, so the per-seat queues are identical by
construction. Workstreams therefore is NOT a new loop and NOT a new mode: it is
(a) seeding different work into different queues, and (b) making that fan-out
conditional. This module holds the pure decision logic for (b) plus the pieces
both halves need, so wiring it in is a predicate at one call site rather than a
parallel implementation of the loop.

Design rules, all learned the hard way elsewhere in this repo:

 * Everything is keyed by SLOT ID, never list index — the same rule meta v2
   already follows, so parking, reassignment and a future seat-list edit can't
   silently re-target someone else's work.
 * A claimed deliverable is verified against the FILESYSTEM before it counts.
   This conversation produced the motivating example: a complete, fluent, past
   tense description of a module that was never written to disk. Artifact
   existence is not quality, but it is the difference between "done" and
   "described".
 * Concurrency safety here is by declared file ownership, not by git worktrees.
   Worktrees would give each seat a DIFFERENT workspace, and one workspace is
   assumed by `confine_to_workspace`, the Files rail, the attachments folder
   and Gemini's image harvest — four things that would need rework before a
   worktree is even safe to hand a seat. Overlap detection is cheap, needs no
   git repo (a default in-session workspace isn't one), and catches the actual
   hazard: two seats writing the same file at the same time.
"""

import os

import outcome

# A task is a plain dict so it round-trips through meta.json untouched.
STATUSES = ("pending", "active", "blocked", "done", "failed")


def make_task(task_id, owner, brief, files=None, deps=None):
    """One unit of parallel work. `owner` is a seat SLOT ID."""
    return {"id": str(task_id), "owner": owner, "brief": brief,
            "files": list(files or []), "deps": [str(d) for d in deps or []],
            "status": "pending", "started_ts": None, "verified": None,
            "replans": 0}


# --------------------------------------------------------- fan-out scoping

def active_owners(tasks):
    """Slot ids currently heads-down on a task."""
    return {t["owner"] for t in tasks if t.get("status") == "active"}


def shares_stream(tasks, speaker_id, listener_id):
    """Should `listener_id` receive `speaker_id`'s reply right now?

    STRICT isolation: a seat working a task broadcasts to nobody, and hears
    nobody, until its task settles. Only the verified settlement summary is
    fanned out (see `summarize`), which is the whole point — if the integrator
    absorbs three workers' in-flight drafts, the context explosion we split the
    work to avoid comes straight back in through the fan-out.

    Two seats with no active task are simply in the main conversation and hear
    each other exactly as before, so turning workstreams on can never silently
    mute an ordinary chat.

    Isolation only ever applies from NOW: a CLI session keeps everything it was
    already told, so this can quiet a seat, never un-tell it something.
    """
    if speaker_id == listener_id:
        return False
    busy = active_owners(tasks)
    return speaker_id not in busy and listener_id not in busy


# ------------------------------------------------------------- scheduling

def unblocked(tasks):
    """Tasks whose dependencies are all done — i.e. runnable right now.

    A dependency naming a task that doesn't exist is treated as UNSATISFIED,
    not ignored: a typo'd id must stall visibly rather than quietly promoting
    work that was meant to wait.
    """
    done = {t["id"] for t in tasks if t.get("status") == "done"}
    known = {t["id"] for t in tasks}
    out = []
    for t in tasks:
        if t.get("status") not in ("pending", "blocked"):
            continue
        if all(d in done for d in t.get("deps", [])) \
                and all(d in known for d in t.get("deps", [])):
            out.append(t)
    return out


def owners_busy(tasks):
    """Slot ids already running a task. One task per seat at a time: a seat is
    a single CLI session with one thread, and that limit is structural."""
    return {t["owner"] for t in tasks if t.get("status") == "active"}


def next_assignments(tasks):
    """(task, owner) pairs that can start now — unblocked, and whose owner
    isn't mid-task. Deterministic order: declaration order, so a resumed run
    schedules identically to the one it is continuing."""
    busy = set(owners_busy(tasks))
    picks = []
    for t in unblocked(tasks):
        if t["owner"] in busy:
            continue
        busy.add(t["owner"])
        picks.append(t)
    return picks


# ---------------------------------------------------------- file ownership

def _norm(path):
    return os.path.normcase(os.path.normpath(path)).rstrip("\\/")


def _overlaps(a, b):
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    return a.startswith(b + os.sep) or b.startswith(a + os.sep)


def file_conflicts(tasks, statuses=("pending", "active", "blocked")):
    """Pairs of tasks that declare overlapping paths — including a file inside
    another task's declared folder. Checked BEFORE dispatch: two seats editing
    one file concurrently is the failure that concurrency buys you, and it is
    cheaper to refuse the plan than to merge the wreckage."""
    live = [t for t in tasks if t.get("status") in statuses]
    hits = []
    for x in range(len(live)):
        for y in range(x + 1, len(live)):
            for pa in live[x].get("files", []):
                for pb in live[y].get("files", []):
                    if _overlaps(pa, pb):
                        hits.append((live[x]["id"], live[y]["id"], pa, pb))
    return hits


# ------------------------------------------------------------ verification

def capability_gate(tasks, writers, statuses=("pending", "blocked")):
    """Keep file-writing tasks off seats whose CLI cannot write files.

    `writers` is the set of slot ids that can actually create files in the
    workspace. This is a HARD capability fact, not a preference: this project
    already learned that names are not a capability map (a Claude seat drew an
    image in code while a GPT seat holding a real image tool waited its turn),
    and a task assigned to a seat that cannot do it doesn't fail loudly — it
    produces a confident report and an empty disk, which is precisely the
    failure `verify_deliverable` exists to catch one step too late.

    A misrouted task is REASSIGNED to the capable seat holding the fewest
    tasks, or REJECTED when no capable seat exists. Either way the action is
    returned so the caller can say so out loud: silently moving work the
    supervisor planned is the kind of helpfulness that makes a plan untrue.
    """
    actions = []
    load = {}
    for t in tasks:
        load[t.get("owner")] = load.get(t.get("owner"), 0) + 1
    for t in tasks:
        if t.get("status") not in statuses or not t.get("files"):
            continue
        if t.get("owner") in writers:
            continue
        candidates = sorted(writers, key=lambda w: (load.get(w, 0), str(w)))
        if not candidates:
            t["status"] = "failed"
            t["verified"] = {"ok": False, "missing": list(t["files"]),
                             "stale": [], "delivered": [], "extra": [],
                             "unverifiable": False}
            actions.append(("rejected", t["id"], t.get("owner"), None))
            continue
        new_owner = candidates[0]
        old = t.get("owner")
        load[old] = max(0, load.get(old, 1) - 1)
        load[new_owner] = load.get(new_owner, 0) + 1
        t["owner"] = new_owner
        actions.append(("reassigned", t["id"], old, new_owner))
    return actions


def serialize_conflicts(tasks):
    """Turn file overlaps into dependencies instead of rejecting the plan.

    Two tasks claiming the same path is a real race, but it is almost never a
    reason to refuse the work — it is a reason to do it in an order. The later
    task (declaration order) gains a dependency on the earlier one, so the plan
    survives and the collision cannot happen. Returns the injected edges.
    """
    added = []
    for a_id, b_id, pa, pb in file_conflicts(tasks):
        by_id = {t["id"]: t for t in tasks}
        first, second = by_id.get(a_id), by_id.get(b_id)
        if not first or not second:
            continue
        if a_id in second.get("deps", []) or b_id in first.get("deps", []):
            continue                      # already ordered, either direction
        second.setdefault("deps", []).append(a_id)
        added.append((b_id, a_id, pa, pb))
    return added


def verify_deliverable(workspace, task, since_ts=None):
    """Did the claimed work actually land on disk?

    Returns {"ok", "delivered", "missing", "stale", "extra"}. A task declaring
    no files is UNVERIFIABLE, not verified: research and discussion tasks are
    legitimate, and pretending a check happened is worse than admitting none
    could. `stale` = the path exists but predates the task starting, which is
    how "I updated X" reads when nothing was written.
    """
    res = {"ok": False, "delivered": [], "missing": [], "stale": [],
           "extra": [], "unverifiable": False}
    files = task.get("files") or []
    if not files:
        res["unverifiable"] = True
        return res
    if not workspace or not os.path.isdir(workspace):
        res["missing"] = list(files)
        return res
    start = since_ts if since_ts is not None else task.get("started_ts")
    for rel in files:
        path = rel if os.path.isabs(rel) else os.path.join(workspace, rel)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            res["missing"].append(rel)
            continue
        if start is not None and mtime + 1e-6 < start:
            res["stale"].append(rel)
        else:
            res["delivered"].append(rel)
    if start is not None:
        claimed = {_norm(p) for p in files}
        seen = outcome.workspace_artifacts(workspace, start)
        res["extra"] = [n for n in seen["names"]
                        if _norm(n) not in claimed][:10]
    res["ok"] = not res["missing"] and not res["stale"]
    return res


def settle(task, workspace, since_ts=None):
    """Record verification on the task and set its status honestly.

    An unverifiable task is marked done (nothing was claimed, so nothing can be
    contradicted); a task that claimed files and didn't produce them is FAILED,
    not done — the whole point is that a confident report cannot promote
    itself.
    """
    res = verify_deliverable(workspace, task, since_ts)
    task["verified"] = res
    task["status"] = "done" if (res["ok"] or res["unverifiable"]) else "failed"
    return task


def summarize(task):
    """One line for the main conversation when a task settles. Downstream seats
    consume this instead of the task's whole intermediate chatter."""
    v = task.get("verified") or {}
    if task.get("status") == "done" and v.get("unverifiable"):
        return f"[{task['id']}] {task['brief']} — reported done (no files " \
               f"were claimed, so nothing was verified)."
    if task.get("status") == "done":
        return f"[{task['id']}] {task['brief']} — done; verified on disk: " \
               f"{', '.join(v.get('delivered') or []) or 'nothing'}."
    problems = []
    if v.get("missing"):
        problems.append("never created: " + ", ".join(v["missing"]))
    if v.get("stale"):
        problems.append("unchanged: " + ", ".join(v["stale"]))
    return f"[{task['id']}] {task['brief']} — NOT delivered (" \
           f"{'; '.join(problems) or 'no verification'})."
