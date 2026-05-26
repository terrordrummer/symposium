"""Host-integration layer (spec §11.4 / §11.5).

These modules are *consumers* of the Symposium public API — they import
and call the frozen runtime (`run_session`, the §5.x models, the
selector / replay / metrics surface) without changing it. Nothing here
is part of the protocol or the schemas.

The MCP server (`symposium.integrations.mcp_server`) depends on the
optional `mcp` SDK and is therefore NOT imported here: importing
`symposium` (or running the CLI) must keep working when the `[mcp]`
extra is not installed. Import `mcp_server` explicitly to use it.
"""
