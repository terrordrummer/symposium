---
description: Symposium deliberation with a LIVE video-call viewer (synthetic portraits, presence states, direct-request arrows, optional per-persona TTS). Pass the problem as $ARGUMENTS.
---

Run a Symposium deliberation and watch it unfold live in the browser.

Do this in TWO steps, in order:

**Step 1 — launch the viewer (background).** Run this Bash command in the
background (it serves the meeting workspace, reads the run's
`transcript.jsonl`, and auto-follows the newest run under `runs/`):

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
it runs, the browser shows: a video-call grid of explicitly synthetic
photographic identities, listening/thinking/speaking states, a live chat
panel, and a static labelled connector for every directed inter-agent request
(`branch_turn` born from a `direct_request`). The user can toggle per-persona
text-to-speech. The portraits are still images, not lip-synced video. When the
local `.symposium` workspace exists, the room title and participant membership
follow Sartori's active-room state; the run selector remains a separate
session-history control. The Sartori panel can manage rooms and memberships;
the conversation composer may start a separate immutable v1 run from the
active room, but never mutates an existing run artifact.

When the final result returns, summarize:

- `outcome` (synthesis | termination)
- the `synthesis_answer` if present
- `generated_agents` (with phase: early_start | runtime)
- `expansions` count
- `panel_final`
- `run_dir`

Then remind the user the viewer is still running (and replays this run on
demand); they can stop it with Ctrl-C in its terminal. Report nothing else.
