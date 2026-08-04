---
description: Symposium deliberation — adaptive, SILENT (no live streaming). One sync result at the end.
---

Invoke the **`mcp__symposium__deliberate_adaptive_muted`** MCP tool with:

- `problem`: the text between the markers below, verbatim
- `provider`: `"cli-auto"`
- `experts`: `[]`

```
$ARGUMENTS
```

`_silent` here means: no per-turn streaming. The tool runs sync and returns one aggregate result at the end. Adaptive behavior (dynamic agent generation, runtime expansion) remains on.

Dispatch the MCP call exactly as above. When it returns, summarize: `final.outcome`, `final.synthesis_answer` if present, `generated_agents`, `expansions`, `panel_final`, `final.run_dir`.
