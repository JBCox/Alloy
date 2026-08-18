# Shared project context — design (v1)

Drafted and implemented 2026-08-16 (Claude Code, not the seats — unlike
ROLES_DESIGN.md this one was not debated in-app). **Status: shipped.** All
file:line references verified against the working tree at implementation time.
The three CLI behaviours in "The bug" were verified by grepping the shipped
binaries, not from documentation or memory.

## The bug

Every seat subprocess runs with `cwd = self.workspace` (`Agent.turn`,
relay.py). Each CLI then applies its OWN project-doc discovery, rooted there:

| Seat | Auto-loads from the working folder |
|------|------------------------------------|
| Claude | `CLAUDE.md` (+ parent dirs, + the user's global `~/.claude/CLAUDE.md`) |
| GPT (codex) | `AGENTS.md`; config keys `project_doc_max_bytes`, `project_doc_fallback_filenames` |
| Gemini (agy) | `AGENTS.md` and `GEMINI.md`; also carries a `contextFileName` setting |

Point a chat at a repo holding only `CLAUDE.md` — which is the normal case, and
was true of this repo — and the Claude seat arrives having read the entire
project while GPT and Gemini arrive blind. Nothing in the transcript says so.

For a three-way design conversation that is the worst failure mode available:
one seat sounds authoritative, two guess, and the reader cannot tell which is
which. It is strictly worse than all three being ignorant.

A second, quieter bug rode along. The one workspace sentence in `preamble()`
read *"You share a scratch workspace (your current directory) ... you may
read/write files there if useful."* Pointed at a real repo that tells three
agents the user's source tree is scratch space — and non-yolo claude holds
`Write`/`Edit` while codex holds `workspace-write`, so the invitation is live.

## Scope

Give every seat the SAME project context, from whatever docs the folder
already has. Out of scope: changing what any seat can DO (this is the roles
rule again — context by instruction, never capability), reading anything
outside the working folder, and modifying the native docs.

## The decision that shapes everything: quote, don't summarize

The obvious design is "summarize the docs into one brief and give it to
everyone." It is the wrong default, because of what the asymmetry actually is:
the seats that are missing out are missing **exactly the bytes the Claude seat
already gets**. Handing them those same bytes closes the gap precisely. A
paraphrase instead gives all three a *third, lossier* thing — and this repo's
CLAUDE.md is 24 KB of hard-won specifics, where a summarizer flattens
"`cmd /c` truncates multi-line args at the first newline" into "be careful with
shims." The value density is in the details.

Verbatim also wins on every other axis: free, no latency at chat start,
deterministic, and unit-testable without spending a token.

So the rule is **size-keyed**:

- Sources total ≤ `BRIEF_MAX` → quoted verbatim (`quote_docs`). No CLI call, no
  file written anywhere.
- Sources larger → one synthesized brief, cached in `<workspace>/AI-CHAT.md`
  and keyed on the sources' sha256, so the cost is one cheap call per *doc
  change*, not per conversation.

`BRIEF_MAX` is small (4000) on purpose — see the argv ceiling below.

## Injection — the one hard rule

Context goes through `preamble()` ONLY, exactly as roles do
(ROLES_DESIGN.md:28). `introduced[i]` resets on `/clear` and `/compact`, so the
preamble is the only text re-injected when a seat's session restarts; anything
pushed through `pending[i]` evaporates at the first `/compact` with no error.

There is a second reason here that roles don't have: **no CLI auto-loads
`AI-CHAT.md`.** Claude reads `CLAUDE.md`, codex reads `AGENTS.md`, agy reads
`AGENTS.md`/`GEMINI.md` — a file named `AI-CHAT.md` is loaded by nobody. A file
alone therefore fixes nothing. It exists as a cache and as something a human can
read, not as the delivery mechanism.

"Just tell the seats to go read a path" fails for the same family of reasons: a
seat may not be able to read outside its workspace, and an instruction that
silently fails for two of three seats *reproduces* the bug.

## The rules that are easy to get wrong

1. **Fingerprint on sha256 only.** Not mtime, not size. `git checkout`,
   `git stash pop`, a file copy and a cloud-sync round trip all move mtime with
   identical bytes; keying off it means spurious staleness, a spurious CLI call
   and a spurious diff in the user's repo.
2. **Never re-scan on resume.** `read_project_context` replays the RECORDED
   text; `brief_drift` reports what changed and the app posts it as a notice.
   Regenerating on continue would hand a later `/clear`'d seat different
   context than its peers were given, with nothing in the transcript saying so
   — silent substitution, the same sin as forging a turn. Recovery is offered
   ("start a new chat to pick up the new version"), never performed. This is
   also precisely why `META_VERSION` did NOT need a bump: old code ignoring the
   new key gives a re-cleared seat *less* context, never wrong continuity.
3. **A failed brief is declared, never faked.** `brief_preamble_block` tells the
   seats synthesis failed and names the docs they can read themselves. An
   invented brief would have three agents reasoning off content no source ever
   contained.
4. **Truncation always says it truncated**, with the byte count and a pointer to
   the real file — which is in their cwd, so the pointer is actionable.
   Unreadable and oversized docs are named too, never silently dropped: a
   missing doc nobody mentions is how a seat ends up wrongly confident.
5. **Never read outside the workspace.** Fixed names, top level, no recursion,
   no parent hops, no `~`. `brief_path` asserts containment. The `..` hop in
   `CodexAgent._lastmsg` that sent codex's `-o` file to `C:\` and silently
   turned every GPT turn into "(no reply)" for a whole conversation is this
   exact bug class.
6. **Never modify the native docs.** "Update it off of theirs" means read them
   and regenerate ours — never write back into `CLAUDE.md`/`AGENTS.md`/
   `GEMINI.md`/`README.md`.
7. **Discovery is never a turn.** No message row, no `thinking` event, no
   `state["turn"]` increment — `store.system` + a `status` emit, like `/clear`
   and `/compact` notices.
8. **The scan set is fixed, the per-seat line is derived.** `project_doc_names()`
   returns a constant: a folder's `CLAUDE.md` is worth quoting to a GPT-only
   chat too, so the scan must not shrink when a provider is unseated or its
   adapter has not landed (grok is registered with `agent=None` today). The
   adapters' `project_docs` attrs drive only the "you already load this one"
   sentence, and `test_brief.test_every_adapter_doc_is_scanned` stops the two
   from drifting apart.
9. **Spawned teams inherit the record**, never re-scan. The child shares the
   parent's workspace, so a mid-conversation doc edit would otherwise give the
   team different context than its parent had, unrecorded.

## The argv ceiling

Windows caps a whole command line at ~32,767 chars, and every adapter passes
the prompt as ONE argv element (claude and codex as the last positional, agy as
`-p message`), with npm shims expanded to `node <long path>.js` eating more.
Preamble growth is therefore genuinely bounded: a fat context block plus a
parallel/free-mode backlog can push a turn over, and it surfaces as a bare
`OSError` from `subprocess.run` that the loop reads as transient — retry, same
wall, "failed twice; skipping this round", every round, for that seat.

Two mitigations: `BRIEF_MAX`/`BRIEF_DOC_MAX` keep the block small, and
`Agent.turn` now catches `OSError`/`ValueError` and re-raises naming the prompt
and command-line lengths, so the failure is legible instead of mysterious.
(`TimeoutExpired` is a `SubprocessError`, not an `OSError` — the timeout path is
untouched.)

## Secrets

Two controls, and one thing that is deliberately NOT a control.

- The verbatim path can only ever quote files inside the workspace, i.e. exactly
  what the Claude seat already receives. Its marginal exposure is zero.
- The synthesis prompt forbids copying credentials, keys, tokens, hostnames and
  private paths into the brief, because that output is written into the user's
  repo and may be committed. This is a prompt-level control, which is a hope
  rather than a guarantee — worth having, not worth relying on.
- The real protection is the read boundary: nothing outside the workspace is
  ever opened. Note the residual risk this does NOT cover: the Claude seat (and
  the Claude synthesizer) also auto-load the user's *global*
  `~/.claude/CLAUDE.md`, which on this machine holds a service-role JWT and SSH
  details. ai-chat cannot stop that; project chats get one preamble line telling
  seats their replies are relayed and transcribed, so not to quote credentials
  out of their own instructions.

## Known gaps

- **Helpers** (`SpawnManager._run_helper`) get a task prompt, not a preamble, so
  they receive no shared context; each inherits cwd and so auto-loads whatever
  its own CLI reads there — the original asymmetry, in miniature. The requesting
  seat has the context and writes the task, so it can pass on what matters.
  Not solved; named rather than half-solved.
- **No `/refresh-context` command.** Deliberate: seats already introduced would
  not get a new preamble, so a refresh would only reach later-cleared seats and
  leave the table inconsistent. Starting a new chat is the honest recovery.
- **agy's `contextFileName` setting is unexplored.** It might let a Gemini seat
  load an arbitrary file natively; the binary references it, and nothing more is
  known.

## Files

`relay.py` — everything above, in one section between `session_project` and
`session_summary`, plus `Agent.project_docs` on each adapter, the `preamble`
kwarg, the `compose_prompt` call, `SessionStore.save`'s `brief` key, and
`--workspace`/`--no-brief` in `main()`.
`app.py` — `_conversation` (build), `_continue` (drift notice), `open_session`
(replay the record).
`ui/index.html` — `#projBrief`, locked by `setSeated`, restored from
`session_summary`.
`tests/test_brief.py` — 34 token-free tests.
