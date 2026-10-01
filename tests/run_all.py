#!/usr/bin/env python3
"""Run the whole test suite:  python3 tests/run_all.py [-v]

Discovers tests/test_*.py, runs them with asyncio debug output suppressed, and
exits non-zero on failure (usable in CI).
"""
from __future__ import annotations

import logging
import os
import sys
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONWARNINGS", "ignore")

if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    logging.disable(logging.CRITICAL)          # engine logs are noisy in tests
    sys.path.insert(0, str(ROOT / "tests"))
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    verbosity = 2 if "-v" in sys.argv else 1
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    total = result.testsRun
    print(f"\n{total} tests | failures={len(result.failures)} errors={len(result.errors)} "
          f"skipped={len(result.skipped)} | {'OK' if result.wasSuccessful() else 'FAILED'}")
    sys.exit(0 if result.wasSuccessful() else 1)
