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

# Legacy (un-namespaced) keys — still read as a fallback so an existing
# single-venue install keeps its credentials after the namespacing change.
_DB_KEY_API = "cred.api_key"
_DB_KEY_SECRET = "cred.api_secret"
_DB_KEY_PASSPHRASE = "cred.passphrase"
_DB_KEY_PREFIX = "cred"
_ASCII = "abcdefghijklmnopqrstuvwxyz0123456789_"


def _slug(text: str) -> str:
    return "".join(ch for ch in str(text or "").strip().lower() if ch in _ASCII)


@dataclass
class Credentials:
    api_key: str
    api_secret: str
    passphrase: str = ""

    def complete(self, needs_passphrase: bool = False) -> bool:
        if needs_passphrase:
            return bool(self.api_key and self.api_secret and self.passphrase)
        return bool(self.api_key and self.api_secret)


class CredentialStore:
    """Wraps the SQLite kv table with AES-GCM encrypted credential storage."""

    def __init__(self, db, secrets_dir: Path, venue_label: str = "MEXC",
                 needs_passphrase: bool = False, venue_id: str = "") -> None:
        self._db = db
        self._cipher = Cipher(secrets_dir)
        self._lock = threading.RLock()
        self._cache: Optional[Credentials] = None
        self.venue_label = venue_label
        self.needs_passphrase = needs_passphrase
        # Namespacing the kv keys by venue means two stores can never read each
        # other's keys even if they are ever handed the same database — the
        # three venues must be isolated by construction, not by convention.
        self.venue_id = _slug(venue_id)
        self._label_id = _slug(venue_label)

    def _key(self, base: str, slug: str = "") -> str:
        slug = slug or self.venue_id
        if slug:
            return f"{_DB_KEY_PREFIX}.{slug}.{base.rsplit('.', 1)[-1]}"
        return base

    def _candidate_keys(self, base: str) -> list:
        keys = [self._key(base)]
        if self._label_id and self._label_id != self.venue_id:
            keys.append(self._key(base, self._label_id))
        keys.append(base)                     # pre-namespacing databases
        return keys

    async def _get(self, base: str) -> Optional[str]:
        for key in self._candidate_keys(base):
            value = await self._db.kv_get(key)
            if value is not None:
                if key != self._key(base):
                    # one-time read-fallback: re-store under the canonical key
                    await self._db.kv_set(self._key(base), value)
                return value
        return None

    async def load(self) -> Optional[Credentials]:
        """Load (and cache) credentials from the encrypted store."""
        enc_key = await self._get(_DB_KEY_API)
        enc_secret = await self._get(_DB_KEY_SECRET)
        enc_pass = await self._get(_DB_KEY_PASSPHRASE)
        if not enc_key or not enc_secret:
            with self._lock:
                self._cache = None
            return None
        creds = Credentials(
            self._cipher.decrypt(enc_key),
            self._cipher.decrypt(enc_secret),
            self._cipher.decrypt(enc_pass) if enc_pass else "",
        )
        with self._lock:
            self._cache = creds
        return creds

    async def save(self, api_key: str, api_secret: str, passphrase: str = "") -> None:
        api_key, api_secret, passphrase = api_key.strip(), api_secret.strip(), (passphrase or "").strip()
        if len(api_key) < 8 or len(api_secret) < 8:
            raise ValueError(f"API key and secret look too short to be valid {self.venue_label} credentials.")
        if self.needs_passphrase and not passphrase:
            raise ValueError(f"{self.venue_label} API keys require the API passphrase as well.")
        await self._db.kv_set(self._key(_DB_KEY_API), self._cipher.encrypt(api_key))
        await self._db.kv_set(self._key(_DB_KEY_SECRET), self._cipher.encrypt(api_secret))
        if passphrase:
            await self._db.kv_set(self._key(_DB_KEY_PASSPHRASE), self._cipher.encrypt(passphrase))
        else:
            await self._db.kv_delete(self._key(_DB_KEY_PASSPHRASE))
        with self._lock:
            self._cache = Credentials(api_key, api_secret, passphrase)

    async def clear(self) -> None:
        await self._db.kv_delete(self._key(_DB_KEY_API))
        await self._db.kv_delete(self._key(_DB_KEY_SECRET))
        await self._db.kv_delete(self._key(_DB_KEY_PASSPHRASE))
        with self._lock:
            self._cache = None

    def snapshot(self) -> Optional[Credentials]:
        with self._lock:
            if self._cache is None:
                return None
            return Credentials(self._cache.api_key, self._cache.api_secret, self._cache.passphrase)

    def masked(self) -> dict:
        """Safe representation for API responses."""
        creds = self.snapshot()
        if not creds:
            return {"configured": False, "api_key_preview": None,
                    "passphrase_set": False, "needs_passphrase": self.needs_passphrase,
                    "complete": False}
        k = creds.api_key
        preview = f"{k[:4]}{'*' * max(0, len(k) - 8)}{k[-4:]}" if len(k) > 12 else "****"
        return {
            "configured": True,
            "api_key_preview": preview,
            "passphrase_set": bool(creds.passphrase),
            "needs_passphrase": self.needs_passphrase,
            "complete": creds.complete(self.needs_passphrase),
        }
