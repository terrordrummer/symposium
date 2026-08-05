---
description: Symposium deliberation. Adaptive + streaming by default; `--strict` fixes the panel, `--silent` returns one sync result. Pass the problem as $ARGUMENTS.
argument-hint: "[--strict] [--silent] <the problem to deliberate>"
---

Read the two flags off the START of `$ARGUMENTS`, then **strip them** — what is
left, verbatim, is the problem.

| flags | MCP tool to invoke |
|---|---|
| *(none)* | `mcp__symposium__deliberate_adaptive` |
| `--silent` | `mcp__symposium__deliberate_adaptive_muted` |
| `--strict` | `mcp__symposium__deliberate` |
| `--strict --silent` | `mcp__symposium__deliberate_muted` |

Invoke the tool from that table with:

- `problem`: the text between the markers below, with the flags stripped, verbatim
- `provider`: `"cli-auto"` (drives local claude/codex CLIs, no API key)
- `experts`: `[]` (let runtime expand the panel if needed) — adaptive modes only,
  omit it under `--strict`

```
$ARGUMENTS
```

**What the flags mean.**

`--strict` — NO dynamic agent generation. The panel is the six built-in
personas, and a capability gap stays a gap instead of being filled by a persona
the runtime invents. Use it when you want the same panel across runs, so two
deliberations are comparable.

`--silent` — no live streaming, one synchronous result at the end. Use it when
nobody is watching: inside a script, or a scheduled run.

For the browser viewer — the circle of personas, the animated direct-request
arrows, the optional per-persona TTS — use `/symposium_deliberate_live`. That is
a different thing, not a flag of this one.

Do NOT analyze the problem, do NOT suggest a panel composition, do NOT pre-process the prompt. Dispatch the MCP call exactly as specified, watch the streamed events flow by (one per persona generated, session_start/end, turn-by-turn messages), and when the final result returns, summarize:

- `outcome` (synthesis | termination)
- the `synthesis_answer` if present
- `generated_agents` (with phase: early_start | runtime)
- `expansions` count
- `panel_final`
- `run_dir`

Report nothing else. The user invoked this command explicitly to launch a deliberation; do not detour into other tools or analysis.

> Fino alla 0.1.0 queste combinazioni erano quattro comandi separati
> (`_silent`, `_strict`, `_strict_silent` accanto a questo): cinque voci di menu
> per due booleani, che ogni sessione doveva leggere e distinguere a ogni turno.
> Sono due flag, e ora si scrivono come due flag.
