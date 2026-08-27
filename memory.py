"""Persistent memory for Alloy - notes that outlive one conversation.

Standalone by the same rule as ``export.py``, ``fork.py`` and ``stats.py``:
stdlib only, imports nothing from relay/app/webview, never raises out of a
public function (errors come back as ``{"error": sentence}``). The caller owns
the root directory and passes it in every call - there is no module-level
default, because ``fork.py``'s gotcha is that a second default is how two
halves of the app silently disagree about where the data lives.

WHERE IT LIVES. ``BASE_DIR/memory/``, a **sibling** of ``sessions/`` and never
a child of it. ``relay.list_sessions`` treats every directory under
``SESSIONS_DIR`` as a chat, so ``sessions/memory/`` would ship a phantom rail
row that sorts by a missing timestamp, pins itself somewhere in the list, and
that the rail's two-step delete would happily ``rmtree``.

WHAT IT IS. Markdown, one file per scope, and the markdown is the ONLY source
of truth - there is no persisted index. The ecosystem's near-consensus design
pairs markdown with a derived index, and at this size that index would buy
nothing and cost a whole staleness class: a few hundred short entries parse in
under a millisecond, so the rollup is computed on every read instead of being
stored and kept in step. The files are meant to be opened and hand-edited; the
parser is deliberately lenient about everything except the ``## <id>`` line
that starts an entry.

SCOPES. A chat reads and writes exactly ONE scope: its project when it has a
custom working folder, and ``global`` when it does not. That is the
confinement - a seat cannot reach another project's notes even if a file in a
cloned dependency talks it into trying. The one crossing is by kind rather
than by scope: a note **Josh** wrote globally is shown to every chat, because
those are his own words about how he wants to work; a note a SEAT wrote
globally stays in the scratch scope it was written in.
"""

import contextlib
import hashlib
import os
import re
import time
import uuid

# ---------------------------------------------------------------- shape ----
MEMORY_VERSION = 1
FILE_HEADER = "<!-- alloy-memory v1 -->"
GLOBAL_SCOPE = "global"

# Who wrote a note, and it changes how the note is treated. `josh` is the only
# kind that crosses from the global scope into a project chat, the only kind
# eviction will not touch, and the only kind a reader should take at face
# value - the other two are somebody's claim at a moment in time.
KIND_JOSH = "josh"
KIND_SEAT = "seat"
KIND_STRUCTURAL = "structural"
KINDS = (KIND_JOSH, KIND_SEAT, KIND_STRUCTURAL)
# Injection order. Josh's own notes outrank anything a machine wrote, and a
# filesystem-verified objective record outranks a seat's prose claim.
KIND_RANK = {KIND_JOSH: 0, KIND_STRUCTURAL: 1, KIND_SEAT: 2}

# ---------------------------------------------------------------- limits ---
ENTRY_TEXT_MAX = 1000      # stored chars per note; a longer one is cut, and said so
ENTRY_LINE_MAX = 300       # rendered chars per note inside a preamble block
FILE_READ_MAX = 400_000    # bytes read from one scope file, then truncated + said so
ENTRIES_MAX = 300          # beyond this, the oldest NON-josh notes are evicted
HITS_MAX = 8               # search results returned, then truncated + said so
LOCK_TIMEOUT_S = 5.0       # how long a writer waits for another writer
LOCK_STALE_S = 30.0        # a lock older than this belonged to a dead process

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_HEAD_RE = re.compile(r"^##\s+(\S+)\s*(.*)$")
_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------- scopes ---
def project_key(workspace):
    """A stable directory-safe key for one working folder.

    The basename alone collides constantly - every second repo has a ``src``,
    an ``app`` or a ``docs`` - and two different folders sharing one memory
    file is the worst failure this store has available, so the key carries a
    sha1 of the canonical path. The basename survives only so a human opening
    ``memory/projects/`` can tell the files apart.
    """
    if not workspace or not isinstance(workspace, str):
        return ""
    try:
        full = os.path.abspath(workspace)
    except (OSError, ValueError, TypeError):
        return ""
    # normcase, not realpath: a project key must not change the day someone
    # replaces a folder with a junction to the same content, and realpath
    # would silently re-key every note written before that.
    # normcase BOTH halves. The first version normcased only the digest,
    # so C:\\AI-CHAT and C:\\ai-chat produced one hash and two
    # basenames - two memory files for one folder, which is the exact
    # collision the sha1 was added to prevent, arriving through the other
    # half of the key. normcase rather than .lower() because it is identity
    # on POSIX, where two differently-cased paths really are two folders.
    full = os.path.normcase(full)
    digest = hashlib.sha1(full.encode("utf-8", "replace")).hexdigest()
    base = _SAFE_KEY.sub("-", os.path.basename(full.rstrip("\\/"))).strip("-")
    return "%s-%s" % ((base[:40] or "project"), digest[:8])


def scope_path(root, scope):
    """The markdown file for one scope, or None when the scope is unusable.

    ``global`` is structurally distinct from any project key because every
    project key ends in ``-<8 hex>`` and this one does not, so a folder
    actually named ``global`` cannot capture the global scope.
    """
    if not root or not scope or not isinstance(scope, str):
        return None
    if scope == GLOBAL_SCOPE:
        return os.path.join(root, "global.md")
    if not _ID_RE.match(scope):
        return None
    return os.path.join(root, "projects", scope + ".md")


# ------------------------------------------------------------ the format ---
# ## <id> | <kind> | <who> | <when>
# <body, until the next "## " or the end of the file>
#
# Only the "## <id>" part is required. Everything after the id on that line is
# OUR rendering of the metadata: a human who mangles it loses the attribution,
# not the note, and the fields that could not be read come back None rather
# than as a plausible default (a note stamped with today's date because its
# real date was unreadable is a lie the reader cannot detect).
_META_SEP = " | "


def _render_entry(entry):
    head = "## " + entry["id"]
    tail = [entry.get("kind") or "", entry.get("who") or "",
            entry.get("when") or ""]
    if any(tail):
        head += _META_SEP + _META_SEP.join(t or "?" for t in tail)
    return head + "\n" + (entry.get("text") or "").strip() + "\n"


def _parse_head(rest):
    """The optional metadata tail of a ``## id ...`` line.

    Read POSITIONALLY. The first version filtered the "?" placeholders out
    before reading, which shifted every later field one to the left: a note
    with no recorded author came back with its DATE as the author and no date
    at all, so it rendered as "- [2026-01-01, undated]" and sorted as if it
    had never been stamped. A placeholder is a position, not noise.

    A first field that is not one of our kinds means this line is not our
    rendering - somebody hand-wrote a title there - so the whole attribution
    is unknown rather than mis-assigned to whatever happens to be in slot 2.
    """
    parts = [p.strip() for p in (rest or "").split(_META_SEP.strip())]
    if parts and not parts[0]:
        parts.pop(0)                      # the separator that follows the id

    def at(i):
        v = parts[i] if i < len(parts) else ""
        return v if v and v != "?" else None

    kind = at(0)
    if kind not in KINDS:
        return None, None, None
    return kind, at(1), at(2)


def parse(text):
    """Markdown -> entry dicts, in file order. Never raises.

    An entry with an empty body is DROPPED: a human who deletes a note's text
    but leaves its header behind meant to delete the note, and keeping a
    headline with nothing under it would inject a blank bullet forever.
    """
    entries, dup, seen = [], [], set()
    cur = None
    for line in (text or "").splitlines():
        m = _HEAD_RE.match(line)
        if m and _ID_RE.match(m.group(1)):
            if cur:
                entries.append(cur)
            kind, who, when = _parse_head(m.group(2))
            cur = {"id": m.group(1), "kind": kind, "who": who, "when": when,
                   "lines": []}
            continue
        if cur is not None:
            cur["lines"].append(line)
    if cur:
        entries.append(cur)
    out = []
    for e in entries:
        body = "\n".join(e.pop("lines")).strip()
        if not body:
            continue
        e["text"] = body
        if e["id"] in seen:
            dup.append(e["id"])
        seen.add(e["id"])
        out.append(e)
    return out, sorted(set(dup))


def render(entries):
    """Entry dicts -> the whole file, header included."""
    body = "\n".join(_render_entry(e) for e in entries)
    return FILE_HEADER + "\n" + _FILE_NOTE + ("\n" + body if body else "\n")


_FILE_NOTE = (
    "<!-- Alloy's memory. Hand-editable: one note per '## <id>' section, and\n"
    "     deleting a section deletes the note. Alloy rewrites this file on\n"
    "     /remember and /forget, so keep the '## <id>' lines intact. -->\n"
)


# ------------------------------------------------------------------- io ----
@contextlib.contextmanager
def _lock(path, timeout=LOCK_TIMEOUT_S):
    """Advisory cross-process lock around one scope file.

    Every write here is read-modify-write, so the playbook's
    last-rename-wins is exactly wrong: two chats in the same project running
    /remember at the same moment would each read the old file and one note
    would vanish with nothing to show it ever existed. Alloy runs several
    chats at once by design (tabs) and several seat threads inside one, so
    this is an ordinary case rather than a race nobody hits.

    A stale lock is BROKEN rather than waited out - a killed process would
    otherwise disable memory permanently - and a lock we could not take is a
    stated failure, never a silent skip.
    """
    # the lock is the FIRST thing written for a brand-new scope, so it - not
    # _atomic_write - is what has to create the directory
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lock = path + ".lock"
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > LOCK_STALE_S:
                    os.unlink(lock)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("another window is writing to this memory "
                                   "file; try again in a moment")
            time.sleep(0.02)
    try:
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        yield
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def _atomic_write(path, text):
    """tmp + replace, with a UNIQUE temp name and a retry.

    Unique because one scope file is shared by every chat in that project,
    exactly like the playbook: a fixed ``.tmp`` lets two writers truncate each
    other's scratch file and both rename it into place. The retry is Windows'
    transient PermissionError when a reader without FILE_SHARE_DELETE (an
    editor with the file open, the search indexer) blocks the rename.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for delay in (0, 0.05, 0.15, 0.3):
            if delay:
                time.sleep(delay)
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                continue
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def load(root, scope):
    """Read one scope. Always returns a dict; ``error`` is a sentence.

    ``truncated`` is a fact about THIS read, not about the file: an oversized
    file is read up to ``FILE_READ_MAX`` and says how much it skipped, so a
    caller can tell a reader that some notes were not consulted rather than
    letting silence imply they do not exist.
    """
    out = {"scope": scope, "path": None, "entries": [], "truncated": False,
           "read": 0, "size": 0, "duplicates": [], "error": None}
    path = scope_path(root, scope)
    if not path:
        out["error"] = "That is not a memory scope."
        return out
    out["path"] = path
    try:
        out["size"] = os.path.getsize(path)
    except OSError:
        return out                      # no file yet is not an error
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(FILE_READ_MAX)
            out["truncated"] = bool(f.read(1))
    except OSError as e:
        out["error"] = "Memory could not be read: %s" % e
        return out
    out["read"] = len(text)
    if out["truncated"]:
        # never split an entry down the middle - a half-read note would be
        # injected as if it were complete
        cut = text.rfind("\n## ")
        if cut > 0:
            text = text[:cut]
    out["entries"], out["duplicates"] = parse(text)
    return out


# ------------------------------------------------------------- mutation ----
def new_id():
    return "m" + uuid.uuid4().hex[:8]


def _stamp():
    return time.strftime("%Y-%m-%d")


def _evict(entries):
    """Keep the file bounded WITHOUT ever discarding something Josh wrote.

    Notes a seat or the Supervisor wrote are cheap to lose - they restate
    things the transcript still holds - so they age out oldest-first. Josh's
    own notes are the whole point of the feature and are never evicted, which
    means a file of nothing but his notes can exceed the cap; that is
    deliberate, and the write says so instead of quietly deleting one.
    """
    if len(entries) <= ENTRIES_MAX:
        return entries, 0
    mine = sum(1 for e in entries if e.get("kind") == KIND_JOSH)
    others = [i for i, e in enumerate(entries) if e.get("kind") != KIND_JOSH]
    dropped = len(others) - max(0, ENTRIES_MAX - mine)
    if dropped <= 0:
        return entries, 0
    cut = set(others[:dropped])          # file order is oldest first
    return [e for i, e in enumerate(entries) if i not in cut], dropped


def remember(root, scope, text, kind=KIND_JOSH, who=None, when=None):
    """Add one note. Returns ``{"ok": True, "id": …, "note": …}`` or an error.

    ``note`` is the human-readable account of anything that happened besides
    the plain add - a truncated body, an eviction - because a write that
    quietly changed what it stored is the same sin as a forged turn.
    """
    text = (text or "").strip()
    if not text:
        return {"error": "A memory needs some text."}
    if kind not in KINDS:
        return {"error": "Unknown memory kind %r." % (kind,)}
    path = scope_path(root, scope)
    if not path:
        return {"error": "That is not a memory scope."}
    notes = []
    if len(text) > ENTRY_TEXT_MAX:
        notes.append("trimmed to %d characters" % ENTRY_TEXT_MAX)
        text = text[:ENTRY_TEXT_MAX].rstrip() + " ..."
    entry = {"id": new_id(), "kind": kind, "who": (who or "").strip() or None,
             "when": (when or _stamp()), "text": text}
    try:
        with _lock(path):
            cur = load(root, scope)
            if cur.get("error"):
                return {"error": cur["error"]}
            if cur["truncated"]:
                return {"error": "This memory file is too large to rewrite "
                                 "safely (%d bytes). Trim it by hand first."
                                 % cur["size"]}
            entries = cur["entries"] + [entry]
            entries, dropped = _evict(entries)
            if dropped:
                notes.append("dropped the %d oldest note%s a seat wrote"
                             % (dropped, "" if dropped == 1 else "s"))
            elif len(entries) > ENTRIES_MAX:
                notes.append("this scope now holds %d notes, all of them "
                             "yours - nothing was dropped" % len(entries))
            _atomic_write(path, render(entries))
    except TimeoutError as e:
        return {"error": "Memory was not saved: %s" % e}
    except OSError as e:
        return {"error": "Memory could not be saved: %s" % e}
    return {"ok": True, "id": entry["id"], "entry": entry,
            "note": "; ".join(notes)}


def forget(root, scope, entry_id):
    """Remove every note carrying ``entry_id``.

    Every, not the first: ids are unique as generated, so a duplicate can only
    come from hand-editing, and a delete that silently picked one of two
    identically-named notes would be the one operation here nobody could undo.
    """
    path = scope_path(root, scope)
    if not path:
        return {"error": "That is not a memory scope."}
    if not entry_id or not _ID_RE.match(str(entry_id)):
        return {"error": "That is not a memory id."}
    try:
        with _lock(path):
            cur = load(root, scope)
            if cur.get("error"):
                return {"error": cur["error"]}
            if cur["truncated"]:
                return {"error": "This memory file is too large to rewrite "
                                 "safely (%d bytes). Trim it by hand first."
                                 % cur["size"]}
            keep = [e for e in cur["entries"] if e["id"] != entry_id]
            removed = len(cur["entries"]) - len(keep)
            if not removed:
                return {"error": "No memory with id %s in this scope."
                                 % entry_id}
            _atomic_write(path, render(keep))
    except TimeoutError as e:
        return {"error": "Memory was not changed: %s" % e}
    except OSError as e:
        return {"error": "Memory could not be changed: %s" % e}
    return {"ok": True, "removed": removed, "remaining": len(keep)}


# -------------------------------------------------------------- reading ----
def _score(entry, terms):
    text = (entry.get("text") or "").lower()
    hits = sum(text.count(t) for t in terms)
    if not hits:
        return 0
    # a note that matches every term beats one that matches one term many
    return hits + 10 * sum(1 for t in terms if t in text)


def search(root, scope, query, limit=HITS_MAX):
    """Plain substring search over one scope, best first.

    No embeddings and no index on purpose: the seats already have grep in
    their own loops, and a second, worse retrieval layer would be duplicated
    machinery plus a fresh staleness class. ``total`` is the honest count so a
    truncated result can say what it left out.
    """
    data = load(root, scope)
    if data.get("error"):
        return {"error": data["error"], "hits": [], "total": 0,
                "scanned": 0, "truncated": bool(data.get("truncated"))}
    terms = [t for t in re.split(r"\W+", (query or "").lower()) if t]
    entries = data["entries"]
    if not terms:
        ranked = list(entries)
    else:
        scored = [(_score(e, terms), i, e) for i, e in enumerate(entries)]
        ranked = [e for s, _, e in sorted(
            (x for x in scored if x[0]), key=lambda x: (-x[0], -x[1]))]
    limit = max(1, int(limit or HITS_MAX))
    return {"hits": ranked[:limit], "total": len(ranked),
            "scanned": len(entries), "truncated": bool(data.get("truncated")),
            "error": None}


def resolve(root, scope, needle):
    """What ``/forget <something>`` most likely meant, best first.

    An exact id is returned alone - that is the confirmed form and nothing
    else should dilute it. Anything else falls back to prefix and then text
    matching, and the CALLER prints the resolved id rather than acting on it.
    """
    data = load(root, scope)
    if data.get("error"):
        return []
    needle = (needle or "").strip()
    if not needle:
        return []
    exact = [e for e in data["entries"] if e["id"] == needle]
    if exact:
        return exact
    low = needle.lower()
    pref = [e for e in data["entries"] if e["id"].lower().startswith(low)]
    if pref:
        return pref
    return search(root, scope, needle, limit=HITS_MAX)["hits"]


def collect(root, scope, cross_global=True):
    """Everything one chat may see, ranked for injection.

    The ONE crossing between scopes, and it is by KIND rather than by scope: a
    note **Josh** wrote in the global file reaches a project chat, because
    those are his own words about how he wants to be worked with; a note a
    SEAT wrote globally does not, because it was written in a scratch chat and
    letting it travel is exactly the cross-project path the confinement
    exists to close.
    """
    data = load(root, scope)
    entries = list(data["entries"])
    errors = [data["error"]] if data.get("error") else []
    truncated = bool(data.get("truncated"))
    # Each entry carries the scope it came FROM, because this list mixes two
    # files and a caller that deletes by id alone would have to guess which
    # one -- and guessing wrong either misses or, with a hand-copied id, hits
    # the other project's note.
    for e in entries:
        e["scope"] = scope
    if cross_global and scope != GLOBAL_SCOPE:
        g = load(root, GLOBAL_SCOPE)
        if g.get("error"):
            errors.append(g["error"])
        truncated = truncated or bool(g.get("truncated"))
        for e in g["entries"]:
            if e.get("kind") == KIND_JOSH:
                e["scope"] = GLOBAL_SCOPE
                entries.append(e)
    # Two stable passes rather than one clever key: newest first WITHIN a
    # kind needs the date descending and the kind ascending, and Python's sort
    # cannot mix directions in one tuple without inverting the string by hand.
    # An undated note sorts last - a hand-written note with no stamp is not
    # evidence of recency.
    entries.sort(key=lambda e: (bool(e.get("when")), str(e.get("when") or "")),
                 reverse=True)
    entries.sort(key=lambda e: KIND_RANK.get(e.get("kind"), 3))
    return {"entries": entries, "truncated": truncated,
            "error": errors[0] if errors else None}
# ------------------------------------------------------------ rendering ----
_WHO_FALLBACK = {KIND_JOSH: "Josh", KIND_STRUCTURAL: "Alloy",
                 KIND_SEAT: "a seat"}


def one_line(entry, line_max=ENTRY_LINE_MAX):
    """One note as a single bullet: attribution, date, collapsed text.

    The attribution is on every line rather than in a heading because the
    lines are interleaved by rank, so a reader cannot tell whose claim a
    given note is from its position. A note whose author was never recorded
    says so ("a seat"), never borrows the one above it.
    """
    text = " ".join((entry.get("text") or "").split())
    if len(text) > line_max:
        text = text[:max(1, line_max - 4)].rstrip() + " ..."
    who = entry.get("who") or _WHO_FALLBACK.get(entry.get("kind"), "unknown")
    return "- [%s, %s] %s" % (who, entry.get("when") or "undated", text)


def render_lines(entries, budget, line_max=ENTRY_LINE_MAX):
    """Fill a character budget in the order given. -> (lines, shown, total).

    The FIRST note is always included, whatever the budget. A block that
    silently rendered nothing because one long note came first would look
    exactly like "there is nothing remembered", which is the one thing this
    feature must never say when it is wrong.
    """
    lines, used = [], 0
    for e in entries:
        line = one_line(e, line_max)
        if lines and used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    return lines, len(lines), len(entries)
