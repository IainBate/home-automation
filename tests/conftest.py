"""Shared pytest fixtures/setup.

scripts/*.py are entry-point modules, not a package - they rely on being
launched with `python3 scripts/foo.py`, which puts scripts/ on sys.path[0]
automatically. Tests need the same thing done explicitly so `import
hotwater_automation_core` (and similar) resolves.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
