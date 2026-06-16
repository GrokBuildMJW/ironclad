"""Pytest bootstrap for the ACK test suite.

Puts ``core/`` on ``sys.path`` so the tests can ``import ack`` (and ``ack.lodestar``)
regardless of the invocation directory.
"""
import sys
from pathlib import Path

# core/ack/tests/conftest.py → parents[2] == core/
CORE_DIR = Path(__file__).resolve().parents[2]
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))
