"""Standalone HTML transcript export — one self-contained file per session.

`export_session(session_dir)` reads a session's messages.jsonl (falling back
to a legacy transcript.md when no structured rows exist) plus meta.json for
header info, and renders ONE self-contained HTML file: inline CSS, no
external resources, no JS required, UTF-8, dark theme. Every piece of dynamic
text is HTML-escaped — seat output is untrusted. The output embeds no export
timestamp, so identical input yields byte-identical output.

This module is fully standalone: stdlib only, no imports from relay/app, so
tests never load the engine.

Errors are returned as {"error": "<sentence>"}, never raised.
Run:  python tests/test_export.py     (token-free)
"""

import datetime
import html
import json
import os

PROVIDER_COLORS = {
    "claude": "#D97757",
    "codex": "#10A37F",
    "gpt": "#10A37F",
    "gemini": "#4796E3",
    "opencode": "#A855F7",
}
JOSH_COLOR = "#C9B896"
SYSTEM_COLOR = "#8a8a8a"
MAX_WIDTH = "860px"

_CSS = """
body{background:#16161c;color:#d8d5cf;font-family:Georgia,'Times New Roman',serif;
 margin:0;padding:2rem 1rem;}
.wrap{max-width:%(maxw)s;margin:0 auto;}
h1{color:#F4B942;font-size:1.6rem;margin:0 0 .3rem;}
.meta-line{color:#8a8a8a;font-size:.85rem;margin-bottom:1rem;}
.chips{margin:0 0 1.5rem;display:flex;flex-wrap:wrap;gap:.4rem;}
.chip{border:1px solid #33333e;border-radius:999px;padding:.15rem .7rem;
 font-size:.78rem;font-family:Verdana,sans-serif;background:#1e1e26;}
.chip b{font-weight:normal;}
.msg{background:#1e1e26;border:1px solid #2a2a35;border-left:3px solid #555;
 border-radius:8px;padding:.7rem 1rem;margin-bottom:.9rem;}
.msg-head{font-family:Verdana,sans-serif;font-size:.85rem;margin-bottom:.4rem;}
.speaker{font-weight:bold;}
.role{color:#8a8a8a;font-style:italic;}
.stamp,.round{color:#8a8a8a;font-family:Verdana,sans-serif;font-size:.75rem;
 border:1px solid #33333e;border-radius:999px;padding:0 .5rem;margin-left:.5rem;}
.caption{color:#8a8a8a;font-size:.78rem;font-family:Verdana,sans-serif;
 font-style:italic;margin-bottom:.4rem;}
.body{white-space:pre-wrap;word-wrap:break-word;line-height:1.45;font-size:.95rem;}
details{margin-top:.5rem;font-family:Verdana,sans-serif;font-size:.78rem;color:#8a8a8a;}
summary{cursor:pointer;}
details li{margin:.15rem 0;}
.pill{display:inline-block;border:1px solid #33333e;border-radius:999px;
 padding:0 .5rem;margin:.5rem .3rem 0 0;font-family:Verdana,sans-serif;
 font-size:.72rem;color:#b5b2ab;background:#18181f;}
.legacy{white-space:pre-wrap;font-family:Consolas,monospace;font-size:.85rem;
 background:#1e1e26;border:1px solid #2a2a35;border-radius:8px;padding:1rem;}
""" % {"maxw": MAX_WIDTH}


def _esc(text):
    """HTML-escape any value for safe interpolation."""
    return html.escape("" if text is None else str(text), quote=True)


def _hhmm(ts):
    """'2026-08-22T14:02:11' -> '14:02'; None when missing/unparseable."""
    try:
        return datetime.datetime.fromisoformat(str(ts)).strftime("%H:%M")
    except (TypeError, ValueError):
        return None


def _read_meta(session_dir):
    try:
        with open(os.path.join(session_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_rows(session_dir):
    """Good rows only; malformed lines are skipped silently."""
    rows = []
    try:
        with open(os.path.join(session_dir, "messages.jsonl"), encoding="utf-8") as f:
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
        return None
    return rows


def _seat_color(row):
    provider = row.get("provider")
    if provider in PROVIDER_COLORS:
        return PROVIDER_COLORS[provider]
    if row.get("speaker") == "josh":
        return JOSH_COLOR
    return SYSTEM_COLOR


def _header(meta, title):
    parts = ["<div class='wrap'>", "<h1>%s</h1>" % _esc(title)]
    stamps = []
    for key in ("created", "updated"):
        if meta.get(key):
            stamps.append("%s %s" % (key.capitalize(), _esc(meta[key])))
    if meta.get("mode"):
        stamps.append("mode: %s" % _esc(meta["mode"]))
    if stamps:
        parts.append("<div class='meta-line'>%s</div>" % " &middot; ".join(stamps))
    seats = [s for s in meta.get("seats") or [] if isinstance(s, dict)]
    if seats:
        chips = []
        for s in seats:
            bits = [_esc(s.get("label")), _esc(s.get("provider")),
                    _esc(s.get("model"))]
            label = " &middot; ".join(b for b in bits if b)
            if s.get("effort"):
                label += " (%s)" % _esc(s["effort"])
            chips.append("<span class='chip'>%s</span>" % label)
        parts.append("<div class='chips'>%s</div>" % "".join(chips))
    return "".join(parts)


def _card(row):
    name = row.get("name") or row.get("speaker") or ""
    head = ["<span class='speaker' style='color:%s'>%s</span>"
            % (_seat_color(row), _esc(name))]
    if row.get("role"):
        head.append("<span class='role'>%s</span>" % _esc(row["role"]))
    stamp = _hhmm(row.get("ts"))
    if stamp:
        head.append("<span class='stamp'>%s</span>" % stamp)
    if row.get("round") is not None:
        head.append("<span class='round'>round %s</span>" % _esc(row["round"]))
    parts = ["<article class='msg'>", "<div class='msg-head'>%s</div>"
             % "".join(head)]
    if row.get("meta"):
        parts.append("<div class='caption'>%s</div>" % _esc(row["meta"]))
    parts.append("<div class='body'>%s</div>" % _esc(row.get("text")))
    activity = row.get("activity")
    if isinstance(activity, list) and activity:
        items, plan = [], None
        for act in activity:
            if not isinstance(act, dict):
                continue
            # The seat's checklist is a state, not a step: it is rendered as
            # a list of its own and left out of the step count, so a reply
            # that only planned does not claim to have worked. The engine
            # keeps exactly one and keeps it last (relay.make_activity_sink).
            todo = act.get("todo")
            if act.get("kind") == "todo" and isinstance(todo, dict) \
                    and isinstance(todo.get("items"), list):
                plan = todo
                continue
            text = act.get("text") or act.get("kind") or ""
            if text:
                items.append("<li>%s</li>" % _esc(text))
        if items:
            parts.append(
                "<details><summary>Worked through %d step%s</summary><ul>%s</ul>"
                "</details>" % (len(items), "" if len(items) == 1 else "s",
                                "".join(items)))
        if plan:
            rows = []
            for it in plan["items"]:
                if not isinstance(it, dict) or not it.get("text"):
                    continue
                mark = {"done": "[x]", "active": "[>]"}.get(it.get("state"),
                                                            "[ ]")
                # _esc on the mark too: no exceptions to "everything in a
                # row is escaped", and one of these marks is a bare ">".
                rows.append("<li>%s %s</li>" % (_esc(mark), _esc(it["text"])))
            if rows:
                parts.append(
                    "<details><summary>Plan &mdash; %s of %s done</summary>"
                    "<ul>%s</ul></details>"
                    % (_esc(plan.get("done")), _esc(plan.get("total")),
                       "".join(rows)))
    # The files this turn produced, verified on disk by the engine before the
    # row was recorded (relay.artifact_descriptors). Rendered here for the
    # same reason the activity block is: an export is the SECOND renderer
    # over these rows, and a field only the app draws is a field an exported
    # transcript quietly loses. Text only, never a link — an export travels
    # away from the machine that holds the workspace, so a path that resolves
    # here would resolve nowhere for whoever it is sent to.
    arts = row.get("artifacts")
    if isinstance(arts, list) and arts:
        items = []
        for art in arts:
            if not isinstance(art, dict) or not art.get("path"):
                continue
            size = art.get("size")
            items.append("<li>%s%s</li>" % (
                _esc(art["path"]),
                (" &middot; %s bytes" % _esc(size))
                if isinstance(size, int) else ""))
        if items:
            parts.append(
                "<details><summary>Produced %d file%s</summary><ul>%s</ul>"
                "</details>" % (len(items), "" if len(items) == 1 else "s",
                                "".join(items)))
    usage = row.get("usage")
    # How full this seat's context was when it finished this turn — its own
    # pill beside the spend ones, because it is a level and not a spend, and
    # because a seat whose CLI reports no tokens can still report this. The
    # share is shown ONLY against a window a CLI actually reported: no
    # measured denominator, no proportion.
    ctx = row.get("context")
    ctx_pill = ""
    if isinstance(ctx, dict):
        used = ctx.get("context_used")
        window = ctx.get("context_window")
        if isinstance(used, int) and used > 0:
            if isinstance(window, int) and window > 0:
                ctx_pill = ("<span class='pill'>context: %s / %s (%d%%)</span>"
                            % (_esc("{:,}".format(used)),
                               _esc("{:,}".format(window)),
                               min(100, round(used * 100.0 / window))))
            else:
                ctx_pill = ("<span class='pill'>context: %s (no window "
                            "reported)</span>" % _esc("{:,}".format(used)))
    if (isinstance(usage, dict) and usage) or ctx_pill:
        pills = "".join("<span class='pill'>%s: %s</span>" % (_esc(k), _esc(v))
                        for k, v in sorted((usage or {}).items())
                        if isinstance(usage, dict))
        parts.append("<div>%s</div>" % (pills + ctx_pill))
    parts.append("</article>")
    return "".join(parts)


def _page(title, header_html, content_html):
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
            "<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n%s\n%s\n</div>\n"
            "</body>\n</html>\n"
            % (_esc(title), _CSS, header_html, content_html))


def export_session(session_dir, out_path=None):
    """Export one session to a self-contained HTML file.

    Returns {"ok": True, "path": ..., "messages": N} on success or
    {"error": "..."} when the session is missing, has nothing to export,
    or the target is unwritable.
    """
    session_dir = os.fspath(session_dir)
    if not os.path.isdir(session_dir):
        return {"error": "Session folder not found: %s" % session_dir}
    rows = _read_rows(session_dir)
    transcript = os.path.join(session_dir, "transcript.md")
    if rows is None and not os.path.isfile(transcript):
        return {"error": ("Nothing to export: %s has neither messages.jsonl "
                          "nor transcript.md") % session_dir}
    if out_path is None:
        out_path = os.path.join(session_dir, "export.html")
    else:
        parent = os.path.dirname(os.path.abspath(out_path))
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            return {"error": "Cannot create output folder: %s" % exc}
    meta = _read_meta(session_dir)
    title = meta.get("title") or os.path.basename(os.path.abspath(session_dir))
    header_html = _header(meta, title)
    if rows is None:
        # Legacy folder: no structured rows, just ship the raw markdown.
        try:
            with open(transcript, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError as exc:
            return {"error": "Cannot read transcript: %s" % exc}
        content = "<pre class='legacy'>%s</pre>" % _esc(raw)
        count = 0
    else:
        content = "\n".join(_card(r) for r in rows)
        count = len(rows)
    page = _page(title, header_html, content)
    out_path = os.path.abspath(out_path)
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(page)
    except OSError as exc:
        return {"error": "Cannot write export file: %s" % exc}
    return {"ok": True, "path": out_path, "messages": count}
