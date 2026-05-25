"""Provider adapters (§6).

The MVP ships a single deterministic adapter: `FakeProvider` (§6.14, §9.1).
Real provider adapters (OpenAI-shaped, Anthropic-shaped) are deferred to
the next milestone; the contract is fixed by `ProviderAdapter`.
"""

from symposium.providers.base import ProviderAdapter
from symposium.providers.fake import FakeProvider

__all__ = ["FakeProvider", "ProviderAdapter"]
