"""Credential store: encrypted persistence + masked presentation.

The dashboard "Settings" button writes API keys here. Live trading is only
enabled when both key + secret are present *and* the operator explicitly flips
the mode to ``live`` (see ``risk_controls`` in the web API).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .crypto import Cipher

_DB_KEY_API = "cred.api_key"
_DB_KEY_SECRET = "cred.api_secret"


@dataclass
class Credentials:
    api_key: str
    api_secret: str

    @property
    def complete(self) -> bool:
        return bool(self.api_key and self.api_secret)


class CredentialStore:
    """Wraps the SQLite kv table with AES-GCM encrypted credential storage."""

    def __init__(self, db, secrets_dir: Path) -> None:
        self._db = db
        self._cipher = Cipher(secrets_dir)
        self._lock = threading.RLock()
        self._cache: Optional[Credentials] = None

    async def load(self) -> Optional[Credentials]:
        """Load (and cache) credentials from the encrypted store."""
        enc_key = await self._db.kv_get(_DB_KEY_API)
        enc_secret = await self._db.kv_get(_DB_KEY_SECRET)
        if not enc_key or not enc_secret:
            with self._lock:
                self._cache = None
            return None
        creds = Credentials(self._cipher.decrypt(enc_key), self._cipher.decrypt(enc_secret))
        with self._lock:
            self._cache = creds
        return creds

    async def save(self, api_key: str, api_secret: str) -> None:
        api_key, api_secret = api_key.strip(), api_secret.strip()
        if len(api_key) < 8 or len(api_secret) < 8:
            raise ValueError("API key and secret look too short to be valid MEXC credentials.")
        await self._db.kv_set(_DB_KEY_API, self._cipher.encrypt(api_key))
        await self._db.kv_set(_DB_KEY_SECRET, self._cipher.encrypt(api_secret))
        with self._lock:
            self._cache = Credentials(api_key, api_secret)

    async def clear(self) -> None:
        await self._db.kv_delete(_DB_KEY_API)
        await self._db.kv_delete(_DB_KEY_SECRET)
        with self._lock:
            self._cache = None

    def snapshot(self) -> Optional[Credentials]:
        with self._lock:
            if self._cache is None:
                return None
            return Credentials(self._cache.api_key, self._cache.api_secret)

    def masked(self) -> dict:
        """Safe representation for API responses."""
        creds = self.snapshot()
        if not creds:
            return {"configured": False, "api_key_preview": None}
        k = creds.api_key
        preview = f"{k[:4]}{'*' * max(0, len(k) - 8)}{k[-4:]}" if len(k) > 12 else "****"
        return {"configured": True, "api_key_preview": preview}
