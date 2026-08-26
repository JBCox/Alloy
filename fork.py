"""Fork (branch) a saved conversation at a chosen message.

`fork_session(session_id, upto_message_id=None, sessions_dir=None)` copies an
existing session folder into a NEW independent sibling folder containing only
the messages up to (and including) the chosen message, with a regenerated
transcript and a sanitized meta.json. Returns {"ok": True, "id": ..., "path":
..., "messages": N} or {"error": sentence}.

Every seat's CLI session id is CLEARED in the fork on purpose: the provider-
side threads still hold the ORIGINAL conversation's memory, and resuming them
from a diverged timeline would claim memory the fork does not have (house rule:
never forge continuity). The fork starts with fresh AI memory at the branch
point.

Standalone stdlib-only module — no imports from relay/app, so tests never load
the engine. The source folder is never modified; on any failure after the copy
begins, the partial copy is removed before returning the error.
"""

import datetime
import json
import os
import shutil

SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
MESSAGES_FILE = "messages.jsonl"
META_FILE = "meta.json"
TRANSCRIPT_FILE = "transcript.md"


def _read_rows(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _clock(ts):
    return ts[11:16] if isinstance(ts, str) and len(ts) >= 16 else ""


def _render_transcript(title, meta, rows):
    out = [f"# AI Chat — {title}\n"]
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    names = " ↔ ".join(
        s.get("label") or s.get("provider") or "?" for s in (meta.get("seats") or []))
    italic = f"*{stamp}"
    if names:
        italic += f" · {names}"
    if meta.get("max"):
        italic += f" · max {meta['max']} rounds"
    out.append(italic + "*\n")
    for row in rows:
        clock = _clock(row.get("ts"))
        if row.get("speaker") == "system":
            out.append(f"\n*{clock} · {row.get('text', '')}*\n")
        else:
            role = row.get("role")
            rmeta = row.get("meta")
            head = row.get("name", "")
            if role:
                head += f" — {role}"
            if rmeta:
                head += f"  · {rmeta}"
            out.append(f"\n## {head}  · {clock}\n\n{row.get('text', '')}\n")
    return "".join(out)


def _unique_dir(parent, base):
    candidate = os.path.join(parent, base)
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(parent, f"{base}-{n}")
        n += 1
    return candidate


def fork_session(session_id, upto_message_id=None, sessions_dir=None):
    """Snapshot a session into a fresh sibling folder up to a message."""
    root = sessions_dir or SESSIONS_DIR
    src = os.path.join(root, session_id)
    if not session_id or not os.path.isdir(src):
        return {"error": f"No session named '{session_id}' exists."}
    meta_path = os.path.join(src, META_FILE)
    if not os.path.isfile(meta_path):
        return {"error": (
            f"'{session_id}' is a legacy transcript-only chat and cannot fork.")}
    msgs_path = os.path.join(src, MESSAGES_FILE)
    try:
        rows = _read_rows(msgs_path)
    except OSError as exc:
        return {"error": f"Could not read '{session_id}' messages: {exc}"}
    except ValueError as exc:
        return {"error": f"Could not parse '{session_id}' messages: {exc}"}

    kept = rows
    if upto_message_id is not None:
        idx = next((i for i, r in enumerate(rows)
                    if r.get("message_id") == upto_message_id), None)
        if idx is None:
            return {"error": (
                f"Message '{upto_message_id}' not found in '{session_id}'.")}
        kept = rows[:idx + 1]

    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError) as exc:
        return {"error": f"Could not read '{session_id}' meta: {exc}"}

    source_id = os.path.basename(os.path.normpath(src))
    new_id = f"{source_id}-fork-{datetime.datetime.now():%H%M%S}"
    dst = _unique_dir(root, new_id)
    new_id = os.path.basename(dst)

    try:
        shutil.copytree(src, dst)
    except OSError as exc:
        shutil.rmtree(dst, ignore_errors=True)
        return {"error": f"Fork of '{session_id}' failed: {exc}"}
    try:
        title = meta.get("title") or new_id
        meta["id"] = new_id
        meta["title"] = f"{title} (fork)"
        meta["ended"] = False
        meta["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        for seat in meta.get("seats") or []:
            seat.pop("session_id", None)
        meta["fork_of"] = {"id": source_id,
                           "message_id": upto_message_id}
        if "children" in meta:
            del meta["children"]
        if "parent" in meta:
            meta["parent"] = None
        with open(os.path.join(dst, MESSAGES_FILE), "w",
                  encoding="utf-8", newline="\n") as f:
            for row in kept:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        with open(os.path.join(dst, TRANSCRIPT_FILE), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(_render_transcript(meta["title"], meta, kept))
        with open(os.path.join(dst, META_FILE), "w",
                  encoding="utf-8", newline="\n") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        for name in ("outcome.json", "say.txt"):
            p = os.path.join(dst, name)
            if os.path.isfile(p):
                os.remove(p)
    except OSError as exc:
        shutil.rmtree(dst, ignore_errors=True)
        return {"error": f"Fork of '{session_id}' failed: {exc}"}
    return {"ok": True, "id": new_id, "path": os.path.abspath(dst),
            "messages": len(kept)}
