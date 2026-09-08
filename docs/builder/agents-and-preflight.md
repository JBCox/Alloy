# Agents and preflight

Purpose: how the two LLM seats and the image seat are configured, what the three preflight tiers mean, what the live probes invoke and cost, when a stored report is reused, and the live status of each provider.

## Seats

A seat is a provider binding (addendum D10). Two LLM seats exist, `A` and `B`, plus the image seat `I`. Roles (planner, builder, reviewer, ...) are assigned per task by the scheduler and never bound to a seat permanently (R-8); see [assignments-and-handoffs.md](assignments-and-handoffs.md).

Each LLM seat is configured under `builder.agents.<label>` in `config.yaml`:

```yaml
builder:
  agents:
    A: {provider: claude, model: fable, reasoning: max, executable: ""}
    B: {provider: codex, model: gpt-6-astra, reasoning: xhigh, executable: ""}
```

- `provider`: `claude`, `codex`, or `gemini` (`builder/providers/__init__.py::make_adapter`; anything else is refused).
- `model`: passed through unchanged (`--model` for claude, `-m` for codex and gemini). Nothing is assumed about a model identifier; a rejected one is a preflight failure quoting the CLI (R-19).
- `reasoning`: `--effort <level>` for claude; `-c model_reasoning_effort="<level>"` for codex; gemini exposes no reasoning flag and shows `not exposed`.
- `executable`: a full path to the CLI binary. Empty means the CLI is found on PATH. A configured path that does not exist is a preflight failure; PATH is never searched as a fallback (`cli_common.resolve_cli`).

npm-installed CLIs (`gemini`, `codex`) put a `.cmd` shim on PATH; the adapter parses the shim and runs `node.exe <bundle.js>` directly so no `cmd.exe` quoting layer touches the arguments. The resolution is printed as `via=npm_shim` with the script path.

Both seats get isolated provider sessions: a UUID Alloy chose (`--session-id` for claude and gemini; codex assigns its own thread id, which Alloy records and resumes with). "Continue the most recent conversation" mechanisms are never used (R-10).

## The three tiers

For every capability in `builder/providers/base.py::CAPABILITIES`, preflight reports three tiers (R-15):

| capability | what it means |
|---|---|
| `image_reading` | the agent sees an image delivered as evidence will be delivered (R-16) |
| `evidence_dir_access` | the packet directory is readable by the agent |
| `read_only_enforcement` | the agent cannot write files under the restricted mode (R-17) |
| `session_create` | an explicit session id is honoured |
| `session_resume` | a later invocation recalls the session |
| `structured_output` | the reply validates against Alloy's schema |
| `model_settings_applied` | model and reasoning as reported by the CLI (R-20) |
| `usage_reporting` | usage fields read from the CLI's own output (R-25) |
| `cancellation` | a cancel kills the whole process tree and confirms it (R-22) |

- `declared`: the adapter's own table (`yes`, `no`, or `probe`). The gemini adapter declares `image_reading`, `session_resume`, and `usage_reporting` as `probe`.
- `local`: `ok` or `missing`. The adapter runs `--version` and its help commands (free), hashes the help text, and checks that every flag it will pass is listed (R-19: flags come from `--help`, never from memory). Missing tokens are printed as `missing in --help: ...`.
- `live`: `not_run`, `verified`, `failed`, or `not_verified`, from the real probes below.

Required for a run (`builder/engine.py::REQUIRED_CAPABILITIES`): `image_reading`, `evidence_dir_access`, `read_only_enforcement`, `session_create`, `structured_output`. A missing required capability blocks the run with a `BLOCKER:` line naming the tier, the evidence, and a remediation (R-21).

## Running preflight

Local only (free):

```text
python -m builder preflight "<wf>"
```

Live (spends provider usage):

```text
python -m builder preflight "<wf>" --live
python -m builder preflight "<wf>" --live --agent A
```

`--agent <label>` (repeatable) probes only that agent; the others keep their stored reports. `--mock <screenplay.json>` probes scripted mocks instead of real CLIs.

Output per agent:

```text
agent A: provider=claude model=fable reasoning=max -> OK
  cli: C:\...\claude.EXE version=2.1.233 via=executable
  effective: model_reported=[...] reasoning_requested=max reasoning_effective=max
  image_reading             declared=yes         local=ok       live=verified  (probe reported {...})
  ...
image seat I: seat=manual vendor=chatgpt model=... -> OK
  declared=manual  local=not_confirmed  live=not_applicable
```

The exit code is `1` when any agent is `BLOCKED`.

## What `preflight --live` invokes

Per agent, in this order (`builder/engine.py::preflight`):

1. Image probe (R-16): Pillow draws a red triangle with the number 7 (`references.make_probe_image`), the packet delivers it exactly as evidence is delivered, and the agent must report `shape`, `color`, and `number` through the `probe_report` schema. A filename in text is not delivery. `structured_output` is marked `verified` when this probe validates.
2. Write probe (R-17): the agent is asked to create a file in its own scratch directory and to report `written`, `attempted_and_refused`, `no_write_tool_available`, or `could_not_attempt`. The file must be absent afterwards. `attempted_and_refused` and `no_write_tool_available` are enforcement evidence; `could_not_attempt` is `not_verified`, never `verified`.
3. Session probe: two invocations; the second must recall a nonce from the first through a resumed session (`session_create`, `session_resume`).
4. Cancellation probe (R-22): a slow prompt is cancelled after `provider_timeouts.cancel_probe_after` seconds (default 8); `verified` means the process tree was confirmed gone. A CLI that answers before the timer fires yields `not_verified` with the reason.

Each probe allows at most one repair round for a malformed reply, so a full live preflight is 5 to 10 calls per agent. Probe invocations do not count toward `max_requests`, but their cost does count toward `max_cost_usd`: it is recorded per agent as `probe_cost` in the report and seeded into the run's tracker later (see [token-efficiency-and-usage.md](token-efficiency-and-usage.md)).

Costs, as recorded in the design from the CLIs' own estimates (12b items 15 and 16; not re-verified here): a claude probe 0.5 to 0.8 USD; two full claude preflights about 5.3 USD in one session; codex reports no cost (unknown, never zero). The GUI's live-preflight confirmation dialog states the same figures.

## Cached reports and invalidation (R-18)

Every preflight stores a `preflight_report` record whose `cache_key` hashes: the resolved CLI path, its `--version` output, the model, the reasoning setting, and the adapter's `settings_signature()` (tools and permission mode for claude; sandbox, approval policy, JSON events for codex; approval mode and `--skip-trust` for gemini; the prompt channel). `load_preflight()` reuses the latest report per agent only when its key still matches. Changing the CLI binary, upgrading it, or changing model, reasoning, or executable invalidates the report, and `start` then stops with the "no current live preflight report" error until `preflight --live` runs again. A caveat about local-only reports satisfying this check is in [launching.md](launching.md).

## Reasoning downgrade reporting (R-20)

`builder/providers/cli_common.py::CliAdapter.invoke` retries once with the documented next level when the CLI's own error mentions the reasoning setting: codex `xhigh` falls to `high` (`REASONING_FALLBACKS = {"xhigh": "high"}`); claude has no fallback table, so a rejected `--effort max` is a preflight failure quoting the CLI; gemini has none. The accepted level is reused for the rest of the process, every result carries `reasoning_requested`, `reasoning_effective`, and `reasoning_rejection`, and the preflight output prints:

```text
  REASONING DOWNGRADE (reported, not silent): requested xhigh -> accepted high: <CLI error text>
```

`status` and the Agents tab show the requested and effective values side by side; nothing is substituted silently.

## Live status per provider (as recorded in the design; not re-verified here)

- claude 2.1.233 with model alias `fable` and `--effort max`: live-verified end to end (image probe, write probe answered `no_write_tool_available`, nonce recalled via `--resume <uuid>`, `total_cost_usd` present as an estimate, cancellation confirmed). The alias resolved to `claude-fable-5` in `modelUsage`. `--output-format json` prints nothing until the reply is complete, so the inactivity timer must not be shorter than the response timeout for claude; the live config used 900 s for both. There is no activity signal for claude.
- codex `gpt-6-astra` at reasoning `xhigh`: live-verified only with the Codex desktop app's binary, `codex-cli 0.153.1` at `%USERPROFILE%\.codex\plugins\.plugin-appserver\codex.exe`, set through `builder.agents.B.executable`. The npm `codex` on PATH (0.147.0) is refused by the API for that model ("requires a newer version of Codex"). Verified with that binary: `-i` images read, writes refused by the read-only sandbox, resume by thread id, usage fields `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `xhigh` accepted without downgrade. codex reports no cost.

```yaml
builder:
  agents:
    B: {provider: codex, model: gpt-6-astra, reasoning: xhigh,
        executable: "C:\\Users\\<you>\\.codex\\plugins\\.plugin-appserver\\codex.exe"}
```

- gemini 0.55.1: unusable under the owner's account (`IneligibleTierError`); nothing about the gemini adapter is live-verified. Its declared `probe` capabilities, its resume-by-UUID behaviour, and its usage field names are unknown until observed.

## Read-only modes per provider (R-17)

- claude: `--tools Read,Glob,Grep --permission-mode plan --strict-mcp-config --disable-slash-commands --add-dir <packet_dir>`; never `--bare` (it reads no OAuth login).
- codex: `-s read-only -C <scratch>` plus `-c sandbox_mode="read-only" -c approval_policy="never"` on every invocation (`codex exec resume` offers no `-s`/`-C`); `--add-dir` (writable) is never passed; `--skip-git-repo-check` always.
- gemini: `--approval-mode plan --include-directories <packet_dir> --skip-trust`.

Restrictions a CLI can only enforce by prompt would be labelled `prompt_only` and unsupported for shared write (`preflight_verdict`); none of the three adapters declares that.

## The image seat I (R-109)

`builder.image_generation` binds the image seat. Only `manual` exists: the owner generates in the ChatGPT or Gemini app from the prompt Alloy writes and imports the files. Its three tiers:

- declared: `manual`, with the vendor the owner intends to use (`vendor`, default `chatgpt`) and the model, if any, as a declaration.
- local: `ok` when the import directory (`builder.concept.import_dir`, default `<workflow_dir>\concept\imports`) is writable and the generate-and-import flow has been confirmed once (the first successful `concept import` records `flow_confirmed_at` on the plan); `not_confirmed` when writable but not yet confirmed; `missing` when not writable (a blocker naming the remediation).
- live: `not_applicable`, with the sentence "provenance is as declared by the owner; consistency of every generated image is checked independently by both LLM seats (R-102, R-109)".

The vendor and model of an import are declarations; Alloy cannot verify them and records them as such. No API key exists anywhere in this build (D12).
