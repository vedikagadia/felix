"""Swappable text-generation client behind one interface.

Provider is selected by LLM_PROVIDER (via Settings):
    gemini   — Google Gemini (google-generativeai), default
    bedrock  — AWS Bedrock Claude (Anthropic messages API via invoke_model)

Callers depend only on the LLMClient ABC; get_llm() picks the implementation
from config. Nothing here touches google-generativeai, boto3, or any network
call at *import* time — those are constructed on first use.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from ...config import get_settings
from ...models import LLMResult


class LLMClient(ABC):
    """One prompt (+ optional system instruction) -> one LLMResult."""

    @abstractmethod
    def complete(self, prompt: str, *, system: str | None = None) -> LLMResult:
        ...

    def stream(self, prompt: str, *, system: str | None = None) -> Iterator[str]:
        """Yield the completion as text deltas as they are generated.

        Default implementation is a non-incremental fallback: it runs the normal
        (blocking) `complete()` and yields the whole text once. Providers that
        support server-side streaming (see GeminiClient) override this to yield
        real token deltas. Callers accumulate the deltas to reconstruct the full
        text, then parse it exactly as they would a `complete()` result.
        """
        yield self.complete(prompt, system=system).text

    @property
    def model_id(self) -> str:
        """The provider model id, for audit logging. Implementations store it as
        `self._model_id`; exposed here so streaming callers (which don't get an
        LLMResult back) can still record which model produced a diagnosis."""
        return getattr(self, "_model_id", "unknown")


def get_llm(provider: str | None = None) -> LLMClient:
    """Construct the configured LLMClient. `provider` overrides Settings if given.

    Raises ValueError for anything other than 'gemini' or 'bedrock'.
    """
    provider = (provider or get_settings().llm_provider).strip().lower()
    if provider == "gemini":
        from .gemini import GeminiClient

        return GeminiClient()
    if provider == "bedrock":
        from .bedrock import BedrockClient

        return BedrockClient()
    raise ValueError(f"Unknown LLM_PROVIDER={provider!r}; expected 'gemini' or 'bedrock'")


__all__ = ["LLMClient", "get_llm"]
