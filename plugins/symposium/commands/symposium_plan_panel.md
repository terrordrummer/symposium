---
description: Pre-deliberation panel planner. Analyzes $ARGUMENTS, lists relevant built-in personas + recommended new ones, lets you create them before launching.
---

You are about to PLAN a Symposium panel composition for the problem below. Do NOT run a deliberation here — this command is a planner preview.

```
$ARGUMENTS
```

Procedure:

## Step 1 — Inventory built-ins
Invoke the **`mcp__symposium__list_personas`** MCP tool. You'll get the six built-ins (logician, visionary, researcher, critic, engineer, coordinator).

## Step 2 — Map each built-in to the problem
For each of the five panel built-ins (skip `coordinator` — it's always present), decide RELEVANT or NOT and give a one-line reason grounded in the problem text. Be honest: if `researcher` adds nothing for a pure formal-logic question, say so.

## Step 3 — Identify capability gaps
Look at what the built-ins together still can't cover. Examples:
- a cryptography question → none of the five carries cryptographic-protocol expertise
- a medical-ethics question → no clinical/IRB persona exists
- an FPGA timing-closure question → no hardware-layer persona exists

For each gap, propose ONE *need* — a 1-2 sentence free-text description suitable for `mcp__symposium__generate_persona`. Don't be greedy: only suggest a gap if the built-ins truly can't cover it.

## Step 4 — Present the plan
Render this exact structure:

```
## Problem
<one-line problem summary>

## Relevant built-ins
- ✓ logician — <reason>
- ✓ visionary — <reason>
- ✗ researcher — <reason for skipping>
- ✓ critic — <reason>
- ✗ engineer — <reason for skipping>
(coordinator is always present)

## Recommended new personas (capability gaps)
1. <need text>
2. <need text>
(or: "No gaps — the built-in panel covers this problem.")

## Suggested experts list for /symposium_deliberate
experts: ["<need 1>", "<need 2>"]
panel:   [<relevant built-in ids>]
```

## Step 5 — Offer to materialize new personas
After presenting the plan, ask the user EXPLICITLY:

> "Vuoi che generi le personas proposte ora per ispezione (via `generate_persona`)? (y / N / modifica la lista)"

Wait for their reply. Do not proceed automatically.

- If **y**: invoke `mcp__symposium__generate_persona` for each gap need in order. For each, show: `id`, `reasoning_scope`, `domain_scope`, `behavioral_constraints` (bulleted), `failure_modes` (bulleted). At the end, present the final suggested deliberate call:
  ```
  /symposium_deliberate <problem>
  ```
  with the experts list inlined as a reminder.
- If **N** or empty: stop. The user will invoke a deliberate command separately.
- If **modifica**: take their adjustments to the experts list and re-render Step 4 with the updated list, then re-ask Step 5.

## Hard rules
- Do not invoke any `deliberate*` MCP tool in this command. The user runs that separately after planning.
- Do not invent personas the user didn't ask for. Stick to the gaps the problem actually surfaces.
- If the problem is fully within the built-in panel's scope, say so plainly — no fabricated gaps.
