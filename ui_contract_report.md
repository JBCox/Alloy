# UI search box ↔ relay session-listing contract audit

Date: 2026-08-23 · Auditor: Lindsey (T3)
Scope: the cross-chat search box in the chats rail (`ui/index.html`, block at
line ~5586 "cross-chat search") checked against the engine contract in
`relay.py`: `search_sessions()` (relay.py:4200), `session_summary()`
(relay.py:4016), `list_sessions()` (relay.py:4091), and the bridge passthrough
in `app.py`. Field names, ordering, and project grouping were compared
field-by-field. Tests referenced: `tests/test_search.py` (22 tests).

## Verdict

The core contract holds: the UI consumes exactly the fields the engine sends,
renders engine order untouched, and the two sides agree on id/title/project/
providers/updated/count/snippets. **Two real mismatches found** (one silent
data loss, one degraded row rendering), plus three minor divergences. Nothing
requires an engine change; all fixes are UI-side or optional engine additions.

## Confirmed conforming

- **Fields consumed vs sent.** Engine returns per chat `{id, title, project,
  updated, providers, count, title_match, snippets[{name, ts, excerpt}]}`.
  UI reads id, title, project, updated, providers, count, snippets[0].excerpt —
  all exist, no invented fields, no typos (`tests/test_search.py` pins the
  shape).
- **Ordering.** Engine: title-match first, then hit-count descending, then
  newest-first inside ties (two stable sorts, relay.py:4249-4250). The UI
  renders `r.chats` in array order without re-sorting — correct; any client
  re-sort would break the documented ranking.
- **Query handling.** UI trims before sending; engine independently collapses
  whitespace and casefolds. The UI uses its own trimmed string for the results
  header, so the collapsed-needle echo difference never surfaces.
- **Error path.** Bridge returns `{error}` for bad input (pinned by
  test_bridge_never_raises_on_bad_input); `renderChatResults` checks `r.error`
  before reading `chats`.
- **Rail ownership.** While a query is active, `refreshChats` re-runs the
  search instead of repainting project groups under the typing hand, and
  clearing restores `renderChats()`. Stale-reply guard (seq + text compare)
  is correct against out-of-order bridge replies.
- **Provider dots.** Search rows map `c.providers[]` (plain provider strings)
  to `var(--<provider>)` with the same grey fallback the ordinary rail rows
  use for `participants[].provider`.

## Mismatches

### M1 — `truncated` flag is dropped (HIGH: silent data loss)
Engine caps results at `SEARCH_CHATS_MAX` (40) and returns `truncated: true`
(relay.py:4251-4252), explicitly designed so "40" never reads as "all".
`renderChatResults` ignores it: the header says "40 chats matching 'q'" with
no indication that more exist. Fix is one line — e.g. append "· more matched"
to the header when `r.truncated`.

### M2 — `title_match` unused → title-only hits render as "0 hits" (MEDIUM)
A chat whose TITLE matches but whose body does not arrives with `count: 0`,
`title_match: true`, empty snippets (engine guarantees this shape;
test_title_match_found_without_body_hit). `searchRow` renders
`${count} hit${...}` and an empty snippet line — the top-ranked result in the
list displays "0 hits" with nothing under it, reading as a glitch rather than
as the best match. Fix: when `c.title_match && !c.count`, label the row
"title match" (and/or put the title into the snippet slot).

### M3 — search results lose spawned-team / legacy markers (LOW)
Ordinary rail rows prefix "↳" for spawned children and style legacy view-only
chats (chatRow, ui/index.html:5479, 5509). The search payload carries neither
`parent` nor `legacy` (it deliberately returns "a small payload … never whole
messages"), so search rows cannot show these markers even if the UI wanted
to. Consequence: a spawned team found via search looks like an ordinary chat
until opened. Options: add `parent`/`legacy` booleans to the engine payload
(additive, shapes preserved), or accept the simplification — recorded here so
it is a decision, not an oversight.

## Minor divergences (deliberate or cosmetic)

- **2-character floor.** The UI refuses queries shorter than 2 chars
  (`chatSearchActive`); the engine happily searches any non-empty needle. So
  single-letter search is an engine capability unreachable from the UI. This
  is documented UI-side as a noise guard — noted so nobody "fixes" either side
  to match the other.
- **`revealInChat` uses `toLowerCase`, engine uses `casefold`.** For ASCII the
  two agree, but casefold equivalences beyond lowercase mapping (e.g. query
  "STRASSE" matching body text "straße") can find a chat that the post-open
  scroll-and-flash then fails to locate — the open still works, only the flash
  misses. Using `.toLowerCase()` was safe-looking and is wrong by exactly this
  much; `String.prototype.toLowerCase` on both sides, or accepting the miss,
  should be an explicit choice.
- **Project grouping absent from results.** Ordinary rail: grouped by
  `s.project` with collapsible headers and a "Needs input" priority group.
  Search results are one flat relevance-ranked list with a count header. That
  is the right call for ranked results (grouping would fight the ranking), but
  the row still shows `· project` inline (`.s-proj`), preserving provenance —
  recorded as an intentional divergence between the two rail states.
- **Snippet `name`/`ts` fields unused.** The engine sends who said it and
  when; the row shows only the excerpt. Free data, no harm.

## Summary

| # | Finding | Severity | Where |
|---|---------|----------|-------|
| M1 | `truncated` ignored — capped lists read complete | HIGH | renderChatResults |
| M2 | title-only match renders "0 hits", empty snippet | MEDIUM | searchRow |
| M3 | no `parent`/`legacy` in payload → missing row markers | LOW | relay.search_sessions payload |
| d1 | 1-char queries impossible from UI (by design) | info | chatSearchActive |
| d2 | revealInChat toLowerCase ≠ engine casefold | low | revealInChat |
| d3 | flat list vs project groups (intentional) | info | renderChatResults |
| d4 | snippet name/ts rendered nowhere | info | searchRow |

All findings verified against relay.py@10339 lines, ui/index.html@6195 lines,
and tests/test_search.py on disk today. No files outside this report were
modified.

## Closing verdict (re-verification after T4/T5)

2026-08-23 · Re-verified by Jackson (T6): `tests/test_search.py` 22 OK,
`tests/test_ui_boot.py` 36 OK, plus direct inspection of the current
ui/index.html@6199 search block and relay.py's search region.

| # | Finding | Verdict |
|---|---------|---------|
| M1 | truncated flag dropped | **RESOLVED** — renderChatResults renders "N chats shown (more match)" when r.truncated (ui/index.html:5641-5647); README "Limits" bullet updated to match |
| M2 | title-only match renders "0 hits", empty snippet | **OPEN** — searchRow still emits `${count} hits` + blank snippet slot for count:0/title_match rows (ui/index.html:5670) |
| M3 | no parent/legacy markers in payload | **OPEN** — engine payload unchanged; accepted simplification until a decision says otherwise |
| d1 | 2-char UI floor | **CLOSED as designed** — floor kept, ✕ now hidden below it too (ui/index.html:5615-5618) |
| d2 | revealInChat toLowerCase ≠ engine casefold | **OPEN (low)** — unchanged; flash may miss on casefold-only equivalences, opening still works |
| d3 | flat results list vs project groups | **CLOSED as intentional** |
| d4 | snippet name/ts unused | **OPEN (info)** — free data, harmless |

Engine-side findings from search_notes.md re-checked and all three remain
**OPEN**: F1 excerpt still cut from original text while positions come from
casefolded text (relay.py:4144, 4194); F3 oversized line added to `seen`
before the bound test, so it and everything after it go unsearched
(relay.py:4135-4137, 4165-4167); F2 fallback condition still `if not parsed`
(relay.py:4227). None changed user-visibly this round, so search_notes.md
stands as written.

## Closing verdict, second pass (T6 re-verification by George)

2026-08-23 · Independent re-check after the file advanced from @6199 to
@6215 between the first verdict above and this pass — two findings moved
underneath it, so their rows are corrected here. Both suites re-run on the
current file by this verifier: `tests/test_search.py` 22 OK,
`tests/test_ui_boot.py` 36 OK. Engine payload confirmed byte-identical to
the audit's field list (no `parent`/`legacy` added).

| # | Finding | Verdict (supersedes first pass where different) |
|---|---------|---------|
| M1 | truncated flag dropped | **RESOLVED** — confirmed at ui/index.html:5641-5647; header reads "N chats shown (more match) — "q"" when r.truncated |
| M2 | title-only match renders "0 hits", empty snippet | **RESOLVED** (was OPEN) — searchRow now renders "title match" in the count slot and "matched the title, not message text" in the snippet slot for count:0/title_match rows (ui/index.html:5674-5687) |
| M3 | no `parent`/`legacy` markers in payload | **RESOLVED without engine change** (was OPEN/accepted) — searchRow joins the rail's own `sessions` summaries for parent/legacy, so spawned teams get the ↳ prefix and legacy styling in results while the payload stays small (ui/index.html:5654-5660, 5671); the option the audit itself offered, and the better one of the two |
| d1 | 2-char UI floor | **CLOSED as designed** — unchanged since first pass |
| d2 | revealInChat toLowerCase ≠ engine casefold | **OPEN (low)** — unchanged |
| d3 | flat results list vs project groups | **CLOSED as intentional** — unchanged |
| d4 | snippet name/ts unused | **OPEN (info)** — partially self-resolved: row tooltip now carries parent provenance, but snippet name/ts remain unrendered |

README.md's "Searching your chats" section updated this pass: the Results
bullet now documents both user-visible changes (title-match label, ↳ marker
in results). The Limits bullet's truncation wording was already accurate.
Engine-side F1/F2/F3 from search_notes.md remain OPEN exactly as the first
pass found them — no relay.py changes verified this round.

## Closing verdict, third pass (d2 fix by Lindsey)

2026-08-23 · d2 claimed and fixed in the cross-chat-search block. `revealInChat(q, excerpt)` now scans in three passes, cheapest first: (1) plain `toLowerCase()` (historical behaviour), (2) a small explicit fold (`_foldForFlash`: ß→ss, œ/æ→oe/ae, ø→o, NFD diacritic strip — the casefold equivalences this app's text actually exercises; folded display forms like "ss" where "ß" was written are accepted artifacts), and (3) the hit's own `snippets[].excerpt` as a fallback needle (ellipsis-stripped, whitespace-collapsed, folded on both sides) — the engine already located THIS message via casefold, so its excerpt finds it even when the query itself folded away. Both scan passes transform needle and haystack with the SAME function. The excerpt rides from `searchRow`'s click handlers through `openChat(id, q, excerpt)` so the post-open flash gets the same fallback.

| # | Finding | Verdict |
|---|---------|---------|
| M1/M2/M3, d1, d3 | as second pass | unchanged — still resolved/closed |
| d2 | revealInChat toLowerCase ≠ engine casefold | **RESOLVED (graceful parity)** (was OPEN) — three-pass scan with fold approximation + excerpt fallback; exact Unicode casefold parity deliberately not attempted (JS has none cheaply); miss now requires all three passes to fail, in which case normal scroll behaviour is kept exactly as before |
| d4 | snippet name/ts unused | **OPEN (info)** — unchanged |

Verified after the edit: `tests/test_ui_boot.py` 36 OK,
`tests/test_search.py` 22 OK against ui/index.html@6237. Engine-side
F1/F2/F3 from search_notes.md are George's claim this wave and are not
re-judged here.
