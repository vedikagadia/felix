"""GeminiClient — Google Gemini via google-generativeai."""

from __future__ import annotations

from typing import Iterator

from ...config import get_settings
from ...models import LLMResult
from . import LLMClient


class GeminiClient(LLMClient):
    def __init__(self):
        self._genai = None  # lazily imported google.generativeai module
        self._configured = False
        settings = get_settings()
        self._api_key = settings.gemini_api_key
        self._model_id = settings.gemini_model_id

    def _get_client(self):
        """Import and configure google.generativeai on first use (no import,
        no network, at module import time)."""
        if self._genai is None:
            import google.generativeai as genai  # imported here so the SDK is only required when gemini is used

            self._genai = genai
        if not self._configured:
            self._genai.configure(api_key=self._api_key)
            self._configured = True
        return self._genai

    def _get_model(self, system: str | None):
        genai = self._get_client()
        kwargs = {}
        if system:
            kwargs["system_instruction"] = system
        return genai.GenerativeModel(self._model_id, **kwargs)

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResult:
        """One generate_content call; pulls token counts from usage_metadata
        when the SDK/response provides them, else leaves them None.

        Request:  model.generate_content(prompt)
        Response: response.text; response.usage_metadata.{prompt_token_count,
                   candidates_token_count} when available.
        """
        model = self._get_model(system)
        response = model.generate_content(prompt)

        # response.text is a @property that RAISES ValueError (not returns None)
        # when the response has no usable parts — e.g. finish_reason SAFETY /
        # RECITATION / MAX_TOKENS. getattr(..., None) does NOT catch that, so
        # guard with try/except and fall through to scanning the candidate's
        # parts directly (which may still hold partial text).
        try:
            text = response.text
        except ValueError:
            text = None
        if not text:
            candidates = getattr(response, "candidates", None) or []
            parts = []
            if candidates:
                content = getattr(candidates[0], "content", None)
                parts = getattr(content, "parts", None) or []
            text = "".join(getattr(p, "text", "") for p in parts)

        usage = getattr(response, "usage_metadata", None)
        input_tokens = getattr(usage, "prompt_token_count", None) if usage else None
        output_tokens = getattr(usage, "candidates_token_count", None) if usage else None

        return LLMResult(
            text=text,
            model=self._model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def stream(self, prompt: str, *, system: str | None = None) -> Iterator[str]:
        """Real server-side streaming: `generate_content(stream=True)` returns an
        iterable of partial responses; each chunk carries the newly-generated
        text. We yield those deltas as they arrive so the caller can forward them
        to the UI live. The concatenation of all yielded deltas equals what
        `complete()` would have returned for the same prompt.

        Each chunk's `.text` can raise ValueError for the same reasons as in
        `complete()` (SAFETY/RECITATION/etc.), so we guard it and fall back to
        scanning the chunk's parts. Empty deltas are skipped.
        """
        model = self._get_model(system)
        response = model.generate_content(prompt, stream=True)
        for chunk in response:
            try:
                delta = chunk.text
            except ValueError:
                delta = None
            if not delta:
                candidates = getattr(chunk, "candidates", None) or []
                parts = []
                if candidates:
                    content = getattr(candidates[0], "content", None)
                    parts = getattr(content, "parts", None) or []
                delta = "".join(getattr(p, "text", "") for p in parts)
            if delta:
                yield delta
