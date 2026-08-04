---
description: Generate one Symposium expert persona for a capability gap. Pass the need as $ARGUMENTS.
---

Invoke the **`mcp__symposium__generate_persona`** MCP tool with:

- `need`: the text between the markers below, verbatim
- `persona_class`: `"domain"`
- `prefer_cli`: `"claude"`

```
$ARGUMENTS
```

Dispatch the call exactly as specified. The tool returns `{"persona": <persona dict>}` or `{"error": ...}`. On success, render the persona compactly:

- `id`
- `reasoning_scope` / `reasoning_style`
- `domain_scope` (the field that makes this a domain persona)
- `behavioral_constraints` (bulleted)
- `failure_modes` (bulleted)

Nothing else. The user can copy this persona into a `panel` of a subsequent deliberate call if they want.
