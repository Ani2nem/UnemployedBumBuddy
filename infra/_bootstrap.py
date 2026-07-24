"""Puts the repo root on sys.path so `src.shared.*` is importable from here.

`src/shared/tables.py` is the frozen contract this whole stack is built
against; importing it directly (instead of re-declaring table/key names)
keeps a rename there a one-line `cdk deploy` here instead of a silent drift.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
