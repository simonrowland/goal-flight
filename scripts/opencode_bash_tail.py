#!/usr/bin/env python3
"""Compatibility shim — prefer scripts/hosts/opencode/bash_tail.py."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "hosts/opencode/bash_tail.py"
sys.argv[0] = str(_TARGET)
runpy.run_path(str(_TARGET), run_name="__main__")
