"""Canonical snapshot hashing used to make idle polling free of ledger events."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize JSON data deterministically, including Unicode ticket text."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


hash_snapshot = snapshot_hash


@dataclass(frozen=True)
class Snapshot:
    payload: Any
    digest: str
    changed: bool

    @property
    def hash(self) -> str:
        return self.digest


class SnapshotTracker:
    """In-memory detector useful for clients that do not persist a DB."""

    def __init__(self, initial_hash: str | None = None) -> None:
        self.previous_hash = initial_hash

    def observe(self, payload: Any) -> Snapshot | None:
        digest = snapshot_hash(payload)
        changed = digest != self.previous_hash
        self.previous_hash = digest
        if not changed:
            return None
        return Snapshot(payload=payload, digest=digest, changed=True)

    def check(self, payload: Any) -> Snapshot:
        digest = snapshot_hash(payload)
        changed = digest != self.previous_hash
        self.previous_hash = digest
        return Snapshot(payload=payload, digest=digest, changed=changed)


__all__ = ["Snapshot", "SnapshotTracker", "canonical_json", "hash_snapshot", "snapshot_hash"]
