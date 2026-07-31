"""Lock the public MCP tool surface — streaming is the DEFAULT.

v1.10.12 flipped the convention: the plain tool names (`deliberate`,
`deliberate_adaptive`) now STREAM each turn live, and the non-streaming
variants are the `*_muted` tools. The old `*_streaming` tool names are
gone. This test pins that surface so a future signature/registration
drift (or an accidental revert to "sync is the default") fails loudly.

The Python *function* names are unchanged — only the registered MCP tool
`name` differs (via `@mcp.tool(name=...)`), which is why the rest of the
suite still imports `deliberate` / `deliberate_streaming` by function name.
"""
from __future__ import annotations

import asyncio

import pytest

mcp_mod = pytest.importorskip("symposium.integrations.mcp_server")


def _registered_tool_names() -> set[str]:
    tools = asyncio.run(mcp_mod.mcp.list_tools())
    return {t.name for t in tools}


def test_deliberate_surface_is_streaming_by_default():
    names = _registered_tool_names()
    deliberate_tools = {n for n in names if n.startswith("deliberate")}
    assert deliberate_tools == {
        "deliberate",
        "deliberate_muted",
        "deliberate_adaptive",
        "deliberate_adaptive_muted",
    }, f"unexpected deliberate* tool surface: {sorted(deliberate_tools)}"


def test_no_legacy_streaming_tool_names():
    names = _registered_tool_names()
    assert "deliberate_streaming" not in names
    assert "deliberate_adaptive_streaming" not in names


def test_streaming_defaults_inject_context_but_muted_do_not():
    """The streaming default tools take an injected `ctx`; the muted tools
    have no `ctx` param (they push no events)."""
    import inspect

    assert "ctx" in inspect.signature(mcp_mod.deliberate_streaming).parameters
    assert "ctx" in inspect.signature(
        mcp_mod.deliberate_adaptive_streaming
    ).parameters
    assert "ctx" not in inspect.signature(mcp_mod.deliberate).parameters
    assert "ctx" not in inspect.signature(mcp_mod.deliberate_adaptive).parameters


def test_every_deliberation_tool_is_async():
    """ALL deliberate* tools plus generate_persona MUST be coroutine
    functions: FastMCP executes a sync tool directly on the event loop,
    so a muted deliberation (up to an hour) would freeze the whole
    server — no get_run_status, no get_version, no pings — until it
    returned. The async bodies push the blocking work off-loop via
    `asyncio.to_thread`."""
    import inspect

    for fn in (
        mcp_mod.deliberate,
        mcp_mod.deliberate_streaming,
        mcp_mod.deliberate_adaptive,
        mcp_mod.deliberate_adaptive_streaming,
        mcp_mod.generate_persona,
    ):
        assert inspect.iscoroutinefunction(fn), (
            f"{fn.__name__} must be async — a sync tool blocks the event loop"
        )
