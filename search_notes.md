# Session-search engine — edge-case probe notes

2026-08-23 · Jackson (T1). Black-box exercise of `relay.search_sessions` and
`relay.list_sessions` (relay.py:4200 / relay.py:4091) against 36 synthetic
scenarios. Fixture sessions lived in a temp dir; `relay.SESSIONS_DIR` was
patched per scenario and restored in `finally` — the real `sessions/` tree
was never read or written. Probe script: temp work dir, not in repo.

## Verified robust (behaves exactly as designed)

- Empty/whitespace queries return a clean empty payload: `""`, `"   "`,
  tabs/newlines, NBSP/em-space, and `None` all → `{chats: [], truncated:
  False}`, no exception, no scan.
- Special characters are LITERAL — no regex interpretation: `[T3]`,
  `(100%`, `$5`, `[a-z]+`, `a|b`, `.*`, and Windows paths (`C:\path`) all
  match only their literal text.
- Case-insensitivity is casefold-based and symmetric: `alloy`/`ALLOY`/
  `AlLoY` agree everywhere; fold EXPANSIONS match both ways (`ß` finds
  STRASSE/Strasse; `Straße`.casefold() = `strasse` finds both, count=2).
- Ranking is correct under load: with 45 matching chats, exactly
  SEARCH_CHATS_MAX (40) return, `truncated: True` is set (computed on the
  full list before slicing), and a title-match ranks first over higher
  body counts.
- Count cap honest: 1500 occurrences report exactly 999
  (SEARCH_COUNT_CAP); snippets cap at 3.
- Relay furniture never matches (`origin == "relay"` rows skipped);
  legacy transcript-only chats ARE searched; missing sessions dir → clean
  empty payload for both functions.
- `list_sessions()` sorts newest-first by `updated`; missing dir → `[]`.
- Fast under abuse: 100k-char query answered in 0.035 s; a 400k-char
  single-message chat scanned in ~1 ms.

## Findings

**F1 — Snippet window misaligns after casefold-expanding characters**
(cosmetic, real). Hit positions are found on the casefolded text but the
excerpt window indexes the ORIGINAL text. Expansion chars (ß→ss, İ etc.)
shift the two by k chars, so the window lands past the match. Repro: one
row of `ß*100 + TARGET-7f3a`, search `target-7f3a` → `count: 1` but the
snippet excerpt contains only ß characters — the hit's context is blank.
Suggested fix (search owner): locate the match in the original text (e.g.
re-scan the line with a case-insensitive regex) before cutting the window,
or build excerpts from the folded text.

**F2 — One parseable row suppresses the transcript fallback, even next to
corrupt rows** (edge, medium-low). The fallback to transcript.md fires only
when ZERO jsonl lines parse (`if not parsed`). A messages.jsonl mixing
broken lines with ONE valid row loses its transcript-only words entirely:
repro = two corrupt lines + `ghostword` in transcript.md → found (hits=1);
append ONE valid unrelated row → same query now hits=0. Partial-write
crashes produce exactly this mixed shape, so this is reachable in practice.
Owner call: merge fallback counts when corrupt_lines > 0, or document the
sharp edge.

**F3 — An oversized line is skipped BEFORE being searched** (by-design
bound, worth knowing). The scanner adds `len(line)` to `seen` then breaks
on `> SEARCH_SCAN_MAX_CHARS` (262144) — so a single oversized line (e.g.
one 400k-char message) is never searched itself AND nothing after it in
that file is either. Repro: 400k-char token line followed by a
`needle-here` line → hits=0. Cheap hardening: test the line first, add to
`seen` after.

**F4 — No Unicode normalization** (limitation). NFC-stored text vs
NFD-composed query (`cafe\u0301`) misses; reverse too. Same IME on both
sides makes this rare; noting it so nobody debugs a "broken" search that is
really a normalization mismatch.

**F5 — Multi-word queries need exact single-space adjacency** (behavior).
The QUERY is whitespace-collapsed; body text is not — `alpha beta` matches
"alpha beta" but NOT "alpha  beta", and phrases wrapping across lines never
match (line/row-scoped scanning). Consistent with title matching; fine for
v1, but a UI hint ("exact phrase") would prevent confusion.

**F6 — Type contract at the relay layer**: truthy non-string queries
(`[1,2]`, `{'a':1}`, `123`, `4.5`, `object()`) raise AttributeError at
`query or ""` → `.split()`; FALSY non-strings (`[]`, `{}`, `0`, `False`)
fall through `or ""` and return a clean empty payload; `None` is clean.
This is fine as long as the app bridge stays the type-defense layer —
tests/test_search.py's bridge-never-raises test pins that. Do not call
`relay.search_sessions` directly with unvalidated input.

**F7 — Echo asymmetry** (cosmetic): empty queries echo the RAW input
(`{"query": "   "}`), non-empty ones echo the NORMALIZED needle
("  ALLOY  " → `"alloy"`). Harmless; the UI header renders the normalized
form.

## Bounds reference (as shipped)

| Constant | Value | Meaning |
|---|---|---|
| SEARCH_SCAN_MAX_CHARS | 262144 | transcript text scanned per chat |
| SEARCH_HITS_PER_CHAT | 3 | snippets kept per chat |
| SEARCH_CHATS_MAX | 40 | chats returned, best first |
| SEARCH_COUNT_CAP | 999 | occurrences counted per chat |
| SEARCH_SNIPPET_CHARS | 150 | excerpt width around a hit |

Probe-verifier note: two early scenario expectations were miscounted on my
side and corrected against the code (ASCII-case equivalence is 3 variants ×
3 occurrences; `Straße` matching 2 occurrences is correct behavior, not a
bug). All other observations above are raw engine output.

## Resolution log (end of wave, 2026-08-23)

- **F1 RESOLVED** (George): both snippet sites now cut the excerpt window
  from the casefolded text itself — positions and window come from one
  string, so a fold expansion can no longer slide a hit out of its own
  context (relay.py `_search_excerpt(low, pos)` at both call sites).
  Accepted artifact: folded display forms (`ss` where `ß` was written).
- **F2 RESOLVED** — and the fix overturned my proposed merge: pinning tests
  showed transcript.md MIRRORS every recorded message (SessionStore writes
  both files), so merging counts would double-charge every intact hit.
  Landed as REPLACE semantics: a degraded log (zero usable rows OR any
  unparseable lines) reads its transcript wholesale; clean files never fall
  back, so relay furniture stays filtered where rows are healthy
  (relay.py:4252-4264). Costs — no per-row snippet names, furniture can
  surface — apply only to already-broken logs. My probe's F2 repro
  ("ghostword" beside corrupt lines) is now findable again.
- **F3 RESOLVED** (George): the byte bound charges every line up front but
  examines every line too — the oversized line gets searched, scanning
  stops after it. Self-review caught and fixed a `continue`-bypass shape
  before landing. My probe's oversized-line repro now finds its needle.
- **F4/F5 remain documented limitations** (no Unicode normalization;
  exact-adjacency multi-word) — unchanged, by design.
- **F6/F7 unchanged** (bridge-layer type defense; query-echo asymmetry).

Verified at close: full suite 41 suites / 1021 tests / 0 failed, re-run
independently by Jackson after the last engine edit.
