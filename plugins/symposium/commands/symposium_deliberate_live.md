---
description: Symposium deliberation with a LIVE browser viewer (circle of personas, glow, animated direct-request arrows, optional per-persona TTS). Pass the problem as $ARGUMENTS.
---

Run a Symposium deliberation and watch it unfold live in the browser.

Do this in TWO steps, in order:

**Step 1 — launch the viewer (background).** Run this Bash command in the
background (it serves a read-only viewer that tails the run's
`transcript.jsonl` and auto-follows the newest run under `runs/`):

```
symposium watch --runs-dir runs
```

It prints a `http://127.0.0.1:<port>/` URL and opens a browser tab. The
viewer starts empty and latches onto the deliberation as soon as Step 2
creates its run directory. Report the URL to the user.

**Step 2 — start the deliberation.** Invoke the
**`mcp__symposium__deliberate_adaptive`** MCP tool with:

- `problem`: the text between the markers below, verbatim
- `provider`: `"cli-auto"` (drives local claude/codex CLIs, no API key)
- `experts`: `[]` (let runtime expand the panel if needed)
- `output_dir`: `"runs"` (MUST match the viewer's `--runs-dir` so the
  viewer can find this run)

```
$ARGUMENTS
```

Do NOT analyze the problem, do NOT suggest a panel composition, do NOT
pre-process the prompt. Dispatch the MCP call exactly as specified. While
it runs, the browser shows: personas on a circle (coordinator at the
centre), a glow on whoever is speaking, a live chat panel, and an animated
labelled arrow for every directed inter-agent request (`branch_turn` born
from a `direct_request`). The user can toggle per-persona text-to-speech.

When the final result returns, summarize:

- `outcome` (synthesis | termination)
- the `synthesis_answer` if present
- `generated_agents` (with phase: early_start | runtime)
- `expansions` count
- `panel_final`
- `run_dir`

Then remind the user the viewer is still running (and replays this run on
demand); they can stop it with Ctrl-C in its terminal. Report nothing else.
