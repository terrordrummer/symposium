---
description: Symposium deliberation — adaptive + streaming (default mode). Pass the problem as $ARGUMENTS.
---

Invoke the **`mcp__symposium__deliberate_adaptive`** MCP tool with:

- `problem`: the text between the markers below, verbatim
- `provider`: `"cli-auto"` (drives local claude/codex CLIs, no API key)
- `experts`: `[]` (let runtime expand the panel if needed)

```
$ARGUMENTS
```

Do NOT analyze the problem, do NOT suggest a panel composition, do NOT pre-process the prompt. Dispatch the MCP call exactly as specified, watch the streamed events flow by (one per persona generated, session_start/end, turn-by-turn messages), and when the final result returns, summarize:

- `outcome` (synthesis | termination)
- the `synthesis_answer` if present
- `generated_agents` (with phase: early_start | runtime)
- `expansions` count
- `panel_final`
- `run_dir`

Report nothing else. The user invoked this command explicitly to launch a deliberation; do not detour into other tools or analysis.
