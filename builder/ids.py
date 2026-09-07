"""Identifiers, hashing, canonical JSON, and timestamps used by every builder record."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Crockford base32: no I, L, O, U, so IDs are unambiguous when read aloud or typed.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32(value: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """Time-sortable unique id: ``<prefix>_<10 chars ms timestamp><8 chars random>``."""
    if not prefix or "_" in prefix:
        raise ValueError(f"invalid id prefix: {prefix!r}")
    return f"{prefix}_{_b32(int(time.time() * 1000), 10)}{_b32(secrets.randbits(40), 8)}"


def utc_now() -> str:
    """ISO 8601 UTC timestamp with microseconds and a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 characters kept as-is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_json(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
