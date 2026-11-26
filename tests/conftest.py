from __future__ import annotations

# Never let a test adopt the developer's real `gh auth token`: Harbor asks gh
# for a token at launch when none is exported, so point it at an executable
# that does not exist. Tests that need a gh login supply their own fake.
import os as _os
_os.environ["HARBOR_GH_EXECUTABLE"] = _os.path.join(_os.path.dirname(__file__), "no-such-gh")

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
