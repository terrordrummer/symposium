---
description: Audit a Symposium panel for skill overlap / boundary clarity / redundancy. Recommends merge / split / drop / refine. Critically reviews any user-requested operation.
---

You are about to AUDIT and (on explicit user confirmation) RESHAPE a Symposium panel. The argument may take several forms — parse it from the text below:

```
$ARGUMENTS
```

Parsing rules:
- **Empty** → audit the six built-ins (the default panel + coordinator).
- **A comma-separated list of built-in IDs** (e.g. `logician, visionary, critic`) → audit that subset.
- **Free-text needs** mixed in (e.g. `logician, visionary, "a cryptography protocol expert"`) → first materialize the custom ones via `mcp__symposium__generate_persona`, then audit the union.
- **A user directive embedded** (e.g. `merge logician and critic`, `split visionary in two`, `drop researcher`, `tighten the boundary between engineer and critic`) → audit FIRST as if no directive were given, THEN critically evaluate the directive against the audit findings.

# Procedure

## Step 1 — Materialize the persona set
- For built-in ids: invoke `mcp__symposium__list_personas` once and pull the matching entries (id, reasoning_scope, role_summary).
- For free-text needs: invoke `mcp__symposium__generate_persona` for each, capturing the full persona dict (reasoning_scope, reasoning_style, behavioral_constraints, failure_modes, domain_scope when present).
- Build an in-memory table.

## Step 2 — Pairwise overlap analysis
For every persona pair (A, B), score overlap on four axes:
- **reasoning_scope** — are they targeting the same kind of thinking? (formal vs lateral vs evidence-based vs adversarial vs implementation are the five canonical built-in axes)
- **behavioral_constraints** — do the bulleted constraints duplicate each other in spirit?
- **failure_modes** — do they share failure modes? (high overlap = they'll fail the same way and won't catch each other's mistakes)
- **domain_scope** — for domain personas, do the scopes touch?

Render a compact overlap matrix. Use ▓ / ░ / · for high / medium / low overlap. Example:

```
            log  vis  res  cri  eng
logician     —    ░    ·    ▓    ·
visionary    ░    —    ·    ·    ░
researcher   ·    ·    —    ░    ·
critic       ▓    ·    ░    —    ·
engineer     ·    ░    ·    ·    —
```

Below the matrix, explain each ▓ cell in one line.

## Step 3 — Boundary diagnostics
Flag each of these patterns with concrete evidence:

- **REDUNDANT** — two personas with ▓ on ≥2 axes. The panel pays for two voices that mostly repeat each other.
- **PARALLEL-FAILURE** — two personas with ▓ on failure_modes. They'll miss the same kind of bug.
- **UNFOCUSED** — one persona whose reasoning_scope is too broad (overlaps lightly with everyone). Needs splitting or sharpening.
- **DEAD-WEIGHT** — a persona with no axis it owns alone given the rest of the panel. Candidate for removal.
- **BOUNDARY-FUZZY** — two personas distinct in principle but whose constraints describe overlapping behaviors. Needs sharper wording.

## Step 4 — Recommendations
Print the recommendations in this exact structure:

```
## Recommendations
- MERGE  <A> + <B> → "<new persona need>"
  Reason: <one-line evidence: which axes overlap>
- SPLIT  <X> → "<need 1>", "<need 2>"
  Reason: <evidence of unfocused scope>
- DROP   <Y>
  Reason: <evidence of dead-weight>
- REFINE <Z>
  Current boundary: <quote>
  Suggested boundary: <revised wording>
  Reason: <evidence of fuzzy boundary>
```

If the panel is clean, say so plainly: "No actionable reshape — overlap is within tolerance, boundaries are crisp."

## Step 5 — Critical review of user directive (if present)

If `$ARGUMENTS` contained an explicit operation (merge / split / drop / refine), produce a separate block:

```
## Critique of requested operation
Requested: <verbatim user directive>

Does the audit support it? [yes | partially | no]

Evidence:
- <bulleted findings from Step 3 that support or contradict the request>

Counter-arguments:
- <bulleted arguments against, if any>

Trade-offs:
- <what is gained / what is lost if executed>

Recommendation: [PROCEED | PROCEED-WITH-CAUTION | RECONSIDER]
<one-paragraph rationale>
```

The recommendation must be GROUNDED in Step 2/3 data, not deferential. If the user asks to merge two personas with ▓ on zero axes, the recommendation should be **RECONSIDER** with specific reasons. If they ask to drop a persona that owns an axis no one else covers, push back.

The audit and the critique are independent: do not water down the audit to match the user's request, and do not water down the critique because the user asked for the operation.

## Step 6 — Decision point

After Step 5 (or after Step 4 if no directive was given), ask explicitly:

> "Decisione: posso applicare la(le) modifica(che) proposte? (y per applicarle tutte / n per fermarmi / `solo: <subset>` per applicare solo alcune / `delega` per autorizzarmi a decidere io)"

Wait for the user's reply. Do NOT execute anything until they reply.

- **y** → apply all `## Recommendations` (and the directive if it survived critique as PROCEED).
- **n** → stop. Report the final panel composition unchanged.
- **solo: <items>** → apply only the listed ones.
- **delega** → you have full authority for this run. Apply your best judgment (typically: every recommendation except RECONSIDER directives).

Execution semantics:
- MERGE: invoke `generate_persona` with a `need` describing the unified scope; show the resulting persona.
- SPLIT: invoke `generate_persona` once per resulting persona; show both.
- DROP: just remove from the panel list.
- REFINE: invoke `generate_persona` with the revised boundary as `need`; show the result.

After execution, render the FINAL panel (ids + one-line scope each) and the suggested invocation:

```
/symposium_deliberate <your problem>
```

with the experts list inlined as a reminder for adaptive deliberation runs.

# Hard rules

- Never apply user-requested operations without first surfacing the audit (Steps 2–4). The user may not have seen the data that should change their mind.
- Never water down the audit. Overlap is overlap.
- Final decision authority is the user's, except under explicit `delega`. Even under `delega`, do not perform operations the critique tagged RECONSIDER without flagging that you're overriding your own recommendation.
- Do not invoke any `deliberate*` MCP tool. That's the user's next step, separately.
