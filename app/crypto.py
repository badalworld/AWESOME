"""Machine-bound encryption for API credentials at rest.

Live-trading API keys are the most sensitive artifact in this project, so:

* The AES-GCM master key lives in ``<data_dir>/.secrets/machine.key`` with mode 0600.
* Credentials are stored encrypted (AES-256-GCM, random 96-bit nonce) in SQLite.
* Plaintext credentials only ever exist in process memory, and are never logged,
  never returned by the HTTP API (only a masked preview is), and never written to disk.
"""
from __future__ import annotations

import base64
import os
import secrets
import stat
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_FILENAME = "machine.key"


def _read_or_create_key(secrets_dir: Path) -> bytes:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(secrets_dir, stat.S_IRWXU)  # 0700
    except OSError:
        pass
    key_path = secrets_dir / KEY_FILENAME
    if key_path.exists():
        raw = key_path.read_bytes().strip()
        try:
            key = base64.urlsafe_b64decode(raw)
            if len(key) == 32:
                return key
        except Exception:  # noqa: BLE001 - corrupted key file -> regenerate
            pass
    key = secrets.token_bytes(32)
    key_path.write_bytes(base64.urlsafe_b64encode(key))
    try:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
    return key


class Cipher:
    """Small AES-GCM helper bound to a machine-local key."""

    def __init__(self, secrets_dir: Path) -> None:
        self.secrets_dir = Path(secrets_dir)
        self._key = _read_or_create_key(self.secrets_dir)
        self._aes = AESGCM(self._key)

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        blob = self._aes.encrypt(nonce, plaintext.encode("utf-8"), b"mexc-bot-cred")
        return base64.urlsafe_b64encode(nonce + blob).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(token.encode("ascii"))
            nonce, blob = raw[:12], raw[12:]
            return self._aes.decrypt(nonce, blob, b"mexc-bot-cred").decode("utf-8")
        except (InvalidTag, ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            raise ValueError(
                "Stored credentials cannot be decrypted with this machine key. "
                "Re-enter your API key in the dashboard Settings tab."
            ) from exc
