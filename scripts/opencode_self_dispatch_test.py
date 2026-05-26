#!/usr/bin/env python3
"""Compatibility shim — prefer scripts/hosts/opencode/self_dispatch_test.py."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "hosts/opencode/self_dispatch_test.py"
sys.argv[0] = str(_TARGET)
runpy.run_path(str(_TARGET), run_name="__main__")
