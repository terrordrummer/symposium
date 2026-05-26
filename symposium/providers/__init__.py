"""Provider adapters (§6).

Built-in adapters:

* `FakeProvider` (§6.14, §9.1) — deterministic test adapter driven by a
  `FakeProviderScript`.
* `OpenAIProvider` (§6.12) — HTTP adapter for OpenAI-Chat-Completions-
  shaped endpoints (real OpenAI plus self-hosted compatible servers).
* `AnthropicProvider` (§6.13) — HTTP adapter for Anthropic-Messages-
  shaped endpoints (real Anthropic plus self-hosted compatible servers).
* `ClaudeCliProvider` — terminal adapter that shells out to the local
  `claude` CLI in print mode (`claude -p`). Needs NO API key: it reuses
  the CLI's existing OAuth/keychain auth. Registered as `claude-cli`.
* `CodexCliProvider` — terminal adapter that shells out to the local
  `codex exec` CLI. Needs NO API key (reuses the CLI's own auth).
  Registered as `codex-cli`.

Discovery happens through `AdapterRegistry` (§6.11). Use
`default_registry()` to get a registry pre-populated with built-in
factories (`openai`, `anthropic`, `claude-cli`, `codex-cli`); `register`
adds custom factories at runtime.
"""

from symposium.providers.anthropic import AnthropicProvider
from symposium.providers.base import ProviderAdapter
from symposium.providers.claude_cli import ClaudeCliProvider
from symposium.providers.codex_cli import CodexCliProvider
from symposium.providers.fake import FakeProvider
from symposium.providers.openai import OpenAIProvider
from symposium.providers.registry import (
    AdapterFactory,
    AdapterRegistry,
    MissingCredentialsError,
    UnknownProviderError,
    default_registry,
    make_fake_factory,
)

__all__ = [
    "AdapterFactory",
    "AdapterRegistry",
    "AnthropicProvider",
    "ClaudeCliProvider",
    "CodexCliProvider",
    "FakeProvider",
    "MissingCredentialsError",
    "OpenAIProvider",
    "ProviderAdapter",
    "UnknownProviderError",
    "default_registry",
    "make_fake_factory",
]
