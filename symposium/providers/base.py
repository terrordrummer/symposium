"""Provider adapter contract (§6.1).

A `ProviderAdapter` is invoked by the runtime with a `ProviderRequest` and
returns a `ProviderResult`. The adapter is responsible for: turning the
canonical request into a vendor call, running any internal tool loop
(§6.4), validating the structured_output against the request's
`expected_output_schema` (§6.5), and returning a result that conforms to
`provider_result.schema.json`.

The MVP ships `FakeProvider` only; real adapters land in a follow-up
milestone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from symposium.models import ProviderRequest, ProviderResult


class ProviderAdapter(ABC):
    """Abstract provider adapter (§6.1).

    Subclasses implement `invoke(request) -> ProviderResult`. The adapter
    is constructed by a factory at session start and is held by the
    runtime for the session's duration.
    """

    name: str = "abstract"

    @abstractmethod
    def invoke(self, request: ProviderRequest) -> ProviderResult:
        """Invoke the provider for one request.

        Returns a `ProviderResult` validated against the v1.0.0 schema.
        Errors (network, content-filter, malformed_response, tool_failure,
        etc.) are reported via `result.error` with a CLOSED `kind` value
        (§6.6). The runtime maps `error.kind` to retry / termination
        decisions per §4.9.
        """
