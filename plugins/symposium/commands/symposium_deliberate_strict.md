---
description: Symposium deliberation — STRICT (fixed panel) + streaming. No dynamic agent generation.
---

Invoke the **`mcp__symposium__deliberate`** MCP tool with:

- `problem`: the text between the markers below, verbatim
- `provider`: `"cli-auto"`

```
$ARGUMENTS
```

`_strict` here means: NO dynamic agent generation. The panel is the six built-in personas; if a session terminates asking for help, the run ends with `user_input_required` / `external_research_required` rather than spawning a new expert.

Do NOT analyze the problem, do NOT pre-process. Dispatch the call exactly as specified, watch the streamed turn-by-turn events, and when the final result returns, summarize: `outcome`, `synthesis_answer` (if any), `transcript_digest`, `run_dir`, `cumulative_usage`, `rounds`.
