# Alloy — brand decisions (locked)

Decided 2026-08-16 in a four-seat ai-chat session (Claude ×2, GPT ×2, Josh deciding).
Concept sheet: branding/alloy-icon-concepts-v1.png

## Name
**Alloy.** Josh built an earlier attempt under this name; keeping it.
Collision risk noted and accepted (Alloy.com, Alloy Automation, an OpenAI TTS voice) — irrelevant for a personal tool.

**The brand rename is decoupled from the filesystem rename.** `C:\ai-chat`, the `ai-chat` CLI
shim (`~\.local\bin\ai-chat.cmd`), and the two skill dirs (`~\.claude\skills\ai-chat\`,
`~\.codex\skills\ai-chat\`) all stay named `ai-chat` for now. Only user-visible surfaces change.

## Tagline
- **"Different metals. One alloy."** → the empty-state `h2` (ui/index.html ~line 540, currently
  "Three minds, one table"). The paragraph directly beneath it already explains the app in plain
  language, so the h2 is free to carry the metaphor.
- **"Many models. One conversation."** → supporting/literal copy only (window title tooltip,
  About string). Not the h2 — it would restate the paragraph below it.

Both lines are count-free on purpose. "Three minds, one table" hardcoded a number the app
outgrew (dynamic seats, duplicate seats, grok already registered in `PROVIDERS`).

## Fixed mark vs. live roster — two different assets
- **Fixed brand mark**: indivisible, encodes no seat count, owns no provider color. Used for the
  `.ico`, the wordmark, About.
- **Live roster cluster**: dynamic, N circles in real provider colors, scales to any seat count.
  This is the in-app participant indicator, NOT the logo.

The current three-dot mark (`ui/index.html:30-34` and `341-345`) is trying to be both and fails
at the first job: three hardcoded `<i>` elements pinned to `--claude`/`--gpt`/`--gemini` via
`nth-child`, so a 5-seat or Claude-vs-Claude run makes the logo lie.

## Icon — trefoil knot
Concept 1 from the sheet. A trefoil is **one continuous closed strand** that merely appears to be
three loops, which makes it the most literal possible drawing of "alloy": elements you cannot pull
apart again. It must be rendered with one uninterrupted material treatment — a single gradient
swept once around the whole strand, never per-lobe color bands, or it reads as "three things."

Rejected: concept 2 (four-point sparkle — generic AI mark, same silhouette as Copilot/Gemini),
concept 3 (swirl — reads as a whirlpool/browser logo; kept only as a compactness reference).

v2 render requirements: flat/vector-friendly, no letterforms, shown at **literal 16px** on both
near-black and near-white backgrounds. If it only works at 256px it's a splash graphic, not an icon.
The taskbar is the real use case — the icon is how Josh finds this app among thirty windows.

## Color
Add a new `--alloy` CSS var (does not exist today — `ui/index.html:10` defines only
`--claude`/`--gpt`/`--gemini`/`--josh`).

| Token | Value | Meaning |
|-------|-------|---------|
| Mark body | `#302A49` | graphite-violet, the cold metal |
| `--alloy` (seam/chrome) | `#F4B942` → `#FFF1C2` | amber-to-ivory molten seam at the trefoil crossings; also the app's chrome accent |
| `--josh` | `#C9B896` (was `#C9A227`) | warm bone — moved off gold so it can't be confused with `--alloy` |
| `--claude` / `--gpt` / `--gemini` | unchanged | participants only |

**The grammar, which is the point:** source colors mean *a participant spoke*. `--alloy` means
*the app itself* (wordmark, buttons, focus rings, round/turn badge). Neither borrows the other.

Magenta/violet-pink was deliberately dropped — it is the house palette of every AI product
shipped in the last two years, and "ownable" was the requirement.

## Icon pipeline
`make_icon.py` is pure Pillow primitives today (`rounded_rectangle`, `polygon`, `ellipse`) drawing
straight to a 7-size `.ico`. The trefoil is a bounded addition: a parametric curve stroked as a
depth-sorted thick polyline with correct over-under crossing order, ~40 lines. Keep it *generated*
rather than a hand-drawn PNG — it regenerates all seven sizes deterministically from one function.
A gradient needs a small numpy/mask composite; that's an addition to the file, not a blocker.

## Implementation order
1. **File/image viewing** (below) — first, so icon candidates can be judged in-app.
2. **Trefoil v2 render** for Josh's approval at real 16px.
3. **`make_icon.py`** encoding the approved geometry, then the text/CSS brand changes.

## File & image viewing (Josh's requirement, 2026-08-16)
Josh should never have to navigate to a folder to see something an agent made.
Two surfaces, one bridge:
- **Inline in chat**: `addMsg` scans message bodies for image paths / markdown image links and
  renders a thumbnail; click opens a lightbox.
- **A Files panel in a rail**: lists everything in the active working folder (newest first) with
  image thumbnails and file-type icons; click previews, a button opens it in the OS.

Hard constraints (these are acceptance criteria, not suggestions):
- The bridge (`api.read_image` / `api.list_workspace_files`) must canonicalize the path and serve
  ONLY files beneath **the active session's workspace**, which is *either* `sessions/<name>/workspace/`
  *or* a folder Josh picked in the UI. Resolve against the live workspace value, never a path
  rebuilt from the session id, and test the `..` escape explicitly — a `..` hop that assumed the
  default layout is exactly the bug that silently broke every GPT turn for a whole conversation.
- Enforce image extension/MIME and a size cap. Return a **data URI** — `file://` will not reliably
  load in WebView2 from the app's own origin.
- Thumbnail-scale bytes first; full resolution only when the lightbox opens.
- **Replay case**: rows in `messages.jsonl` outlive the files they mention, and a reopened old chat
  points at a different workspace. Missing or out-of-bounds files must render a quiet
  "image no longer available" placeholder — never a broken tag, never a silent empty row.
