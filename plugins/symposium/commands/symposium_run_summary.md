---
description: Summarize a persisted Symposium run (outcome, digest, replay-ok, metrics). Pass run_dir as $ARGUMENTS.
---

Invoke the **`mcp__symposium__get_run_summary`** MCP tool with:

- `run_dir`: the path between the markers below, verbatim

```
$ARGUMENTS
```

The tool loads `<run_dir>/artifact.json`, recomputes the §7.9 MVP metrics, verifies the §7.5 transcript replay, and returns
`{outcome, transcript_digest, digest_replay_ok, tokens, cost, rounds, selected_agents, termination_reason?}`.

Render the result compactly. Highlight `digest_replay_ok` (true / false) on its own line — it is the byte-identical replay check.
