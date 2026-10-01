"""Shared test helpers.

Tests must never touch the *live* runtime directory: they get their own copy of
config.toml whose relative paths point into a throwaway temp directory, so a
user's dashboard settings cannot change the outcome of the test suite (and the
suite cannot corrupt the live bot's state).
"""
from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402

_TEMPDIRS: list[Path] = []


def temp_dir(prefix: str = "ao-test-") -> Path:
    p = Path(tempfile.mkdtemp(prefix=prefix))
    _TEMPDIRS.append(p)
    return p


@atexit.register
def _cleanup() -> None:  # pragma: no cover - process teardown
    for p in _TEMPDIRS:
        shutil.rmtree(p, ignore_errors=True)


def isolated_config(data_dir: Path | None = None) -> Config:
    """A Config identical to the shipped one, but fully isolated on disk."""
    data_dir = Path(data_dir or (temp_dir() / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = data_dir.parent / "config.toml"
    text = (ROOT / "config.toml").read_text()
    marker = 'data_dir = "data"'
    if marker in text:
        text = text.replace(marker, f'data_dir = "{data_dir}"')
    cfg_path.write_text(text)
    return Config(cfg_path)
