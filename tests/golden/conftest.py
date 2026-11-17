"""Golden-file assertions for Harbor's operator- and builder-facing contracts.

Each golden test renders a deterministic value (text or JSON) and compares it
byte-for-byte with a checked-in snapshot under ``tests/golden/snapshots``.

A snapshot changes only on purpose.  Regenerate after an intended contract
change with::

    HARBOR_UPDATE_GOLDEN=1 uv run pytest tests/golden

and review the snapshot diff like any other code change: these files are the
exact text Harbor shows operators and hands to builders.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

SNAPSHOTS = Path(__file__).with_name("snapshots")


class Golden:
    def __init__(self, update: bool) -> None:
        self.update = update

    def _check(self, name: str, rendered: str) -> None:
        path = SNAPSHOTS / name
        if self.update:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            return
        if not path.exists():
            pytest.fail(f"missing golden snapshot {name}; run with HARBOR_UPDATE_GOLDEN=1 to create it")
        expected = path.read_text(encoding="utf-8")
        assert rendered == expected, f"golden snapshot {name} changed; review and regenerate if intended"

    def text(self, name: str, value: str) -> None:
        self._check(name, value if value.endswith("\n") else value + "\n")

    def json(self, name: str, value: Any) -> None:
        self._check(name, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


@pytest.fixture
def golden() -> Golden:
    return Golden(os.environ.get("HARBOR_UPDATE_GOLDEN") == "1")
