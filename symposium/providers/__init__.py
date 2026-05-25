"""Provider adapters (§6).

Built-in adapters:

* `FakeProvider` (§6.14, §9.1) — deterministic test adapter driven by a
  `FakeProviderScript`.
* `OpenAIProvider` (§6.12) — HTTP adapter for OpenAI-Chat-Completions-
  shaped endpoints (real OpenAI plus self-hosted compatible servers).

Discovery happens through `AdapterRegistry` (§6.11). Use
`default_registry()` to get a registry pre-populated with built-in
factories; `register` adds custom factories at runtime.
"""

from symposium.providers.base import ProviderAdapter
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
    "FakeProvider",
    "MissingCredentialsError",
    "OpenAIProvider",
    "ProviderAdapter",
    "UnknownProviderError",
    "default_registry",
    "make_fake_factory",
]
