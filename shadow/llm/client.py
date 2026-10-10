"""Gemini access. One method: ask for JSON that follows a schema.

Uses the Gemini REST API directly, so there is no SDK to install or to stall on a
network with broken IPv6.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Optional, Protocol

from .. import net

# Fast, and usable on a free-tier key. With a billed key, SHADOW_GEMINI_MODEL=gemini-3.8-flash
# gives a stronger model (its free tier allows only 5 requests a minute).
DEFAULT_MODEL = "gemini-3.5-flash-lite"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RATE_WAIT = 45.0  # longest we wait out a per-minute rate limit before giving up


def _retry_delay(body: str) -> Optional[float]:
    """Seconds the API asks us to wait after a rate-limit reply, if it says."""
    try:
        for detail in json.loads(body)["error"].get("details", []):
            delay = detail.get("retryDelay")
            if delay:
                return float(str(delay).rstrip("s"))
    except (ValueError, KeyError, TypeError):
        pass
    return None


class LlmClient(Protocol):
    def json(self, system: str, user: str, schema: dict, files: Optional[list] = None) -> dict:
        """Return the model's reply parsed as JSON."""


class GeminiClient:
    """The key comes from GEMINI_API_KEY; the model from SHADOW_GEMINI_MODEL."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, timeout: float = 90.0) -> None:
        self._key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self._key:
            raise RuntimeError("no Gemini key; set GEMINI_API_KEY")
        self.model = model or os.environ.get("SHADOW_GEMINI_MODEL", DEFAULT_MODEL)
        self.timeout = timeout
        self.calls = 0
        self.tokens = 0

    def json(self, system: str, user: str, schema: dict, files: Optional[list] = None) -> dict:
        parts: list[dict] = [{"text": user}]
        for mime, data in files or []:
            parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}})
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                 "responseSchema": schema},
        }
        url = ENDPOINT.format(model=self.model)
        reply = None
        for attempt in range(3):
            try:
                reply = net.request("POST", url, body, {"x-goog-api-key": self._key}, self.timeout)
                break
            except net.HttpError as exc:
                if exc.status not in RETRY_STATUS or attempt == 2:
                    raise RuntimeError(f"Gemini request failed: {exc}") from None
                wait = _retry_delay(exc.body) if exc.status == 429 else None
                time.sleep(min(wait, MAX_RATE_WAIT) + 1 if wait else 1.5 * (attempt + 1))
            except OSError as exc:
                if attempt == 2:
                    raise RuntimeError(f"could not reach Gemini: {exc}") from None
                time.sleep(1.5 * (attempt + 1))
        self.calls += 1
        self.tokens += (reply.get("usageMetadata") or {}).get("totalTokenCount", 0)
        candidates = reply.get("candidates") or []
        text = "".join(part.get("text", "") for part in
                       (candidates[0].get("content", {}).get("parts") or [])) if candidates else ""
        if not text.strip():
            reason = (candidates[0].get("finishReason") if candidates else None) or reply.get("promptFeedback")
            raise RuntimeError(f"Gemini returned no text ({reason})")
        return json.loads(text)


def default_client() -> Optional[LlmClient]:
    """A Gemini client if a key is configured, else None."""
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return GeminiClient()
    return None
