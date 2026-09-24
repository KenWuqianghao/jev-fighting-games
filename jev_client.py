"""Small client for the Jev System One API (POST /v1/systemone).

Jev does not generate text. It reads a `state` and answers typed
questions (choice / score / noul) in one round trip.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_MODEL = "jev-latest"
RETRY_STATUSES = {429, 529}


class JevError(RuntimeError):
    pass


class JevClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_s: float = 5.0,
    ):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise JevError("Set TYPESAFE_API_KEY in the environment or .env")
        self.url = f"{base_url.rstrip('/')}/systemone"
        self.model = model
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )

    def decide(
        self, state: Any, questions: dict[str, dict], retries: int = 2
    ) -> tuple[dict[str, dict], float, dict]:
        """Return (answers, latency_ms, usage)."""
        body = {"model": self.model, "state": state, "questions": questions}
        for attempt in range(retries + 1):
            t0 = time.perf_counter()
            resp = self.session.post(self.url, json=body, timeout=self.timeout_s)
            latency_ms = (time.perf_counter() - t0) * 1000.0
            if resp.status_code == 200:
                data = resp.json()
                return data["answers"], latency_ms, data.get("usage", {})
            if resp.status_code in RETRY_STATUSES and attempt < retries:
                wait = float(resp.headers.get("retry-after", 0.25 * (attempt + 1)))
                time.sleep(wait)
                continue
            raise JevError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        raise JevError("unreachable")

    def warm_up(self) -> float:
        """One throwaway call so the TLS session is open. Returns ms."""
        _, ms, _ = self.decide(
            "warm up",
            {"ok": {"type": "noul", "instructions": "Is this a warm-up call?"}},
        )
        return ms
