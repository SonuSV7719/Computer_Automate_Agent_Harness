"""Gemini access. One method: ask for JSON that follows a schema."""
from __future__ import annotations

import json
import os
from typing import Optional, Protocol

DEFAULT_MODEL = "gemini-3.8-flash"


class LlmClient(Protocol):
    def json(self, system: str, user: str, schema: dict, files: Optional[list] = None) -> dict:
        """Return the model's reply parsed as JSON."""


class GeminiClient:
    """Calls Gemini through the google-genai SDK. The key comes from GEMINI_API_KEY."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        from google import genai

        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("no Gemini key; set GEMINI_API_KEY")
        self._client = genai.Client(api_key=key)
        self.model = model or os.environ.get("SHADOW_GEMINI_MODEL", DEFAULT_MODEL)
        self.calls = 0

    def json(self, system: str, user: str, schema: dict, files: Optional[list] = None) -> dict:
        from google.genai import types

        parts = [types.Part.from_text(text=user)]
        for mime, data in files or []:
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        self.calls += 1
        response = self._client.models.generate_content(
            model=self.model,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                system_instruction=system, temperature=0,
                response_mime_type="application/json", response_schema=schema),
        )
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("Gemini returned an empty reply")
        return json.loads(text)


def default_client() -> Optional[LlmClient]:
    """A Gemini client if a key is configured, else None."""
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return GeminiClient()
    return None
