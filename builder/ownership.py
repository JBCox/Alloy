"""Editing ownership enforced at the execution boundary (spec R-41, R-44, R-45).

One holder per resource. Every operation presents an ownership token and the base revision it
expects; ``check`` refuses unknown, released, mismatched, and stale tokens before any Blender process
starts. A handoff releases the old token (which can never be reused) and grants the next holder a
new token bound to the verified revision.
"""
from __future__ import annotations

import secrets
from typing import Any

from .ids import utc_now
from .records import Record
from .store import Store


class OwnershipError(Exception):
    pass


class OwnershipManager:
    def __init__(self, store: Store, run_id: str | None = None):
        self.store = store
        self.run_id = run_id

    def current(self, resource: str) -> Record | None:
        held = [r for r in self.store.list("ownership", state="held") if r.data.get("resource") == resource]
        return held[-1] if held else None

    def by_token(self, token: str) -> Record | None:
        for rec in self.store.list("ownership"):
            if rec.data.get("token") == token:
                return rec
        return None

    def acquire(self, resource: str, holder: str, base_revision_id: str | None, *, actor: str, reason: str) -> Record:
        cur = self.current(resource)
        if cur is not None:
            if cur.data.get("holder") == holder:
                return cur
            raise OwnershipError(f"{resource} is held by {cur.data.get('holder')} (since {cur.data.get('granted_at')}); "
                                 f"{holder} must wait for release or handoff")
        rec = Record.new("ownership", {
            "resource": resource, "holder": holder, "token": secrets.token_hex(16),
            "base_revision_id": base_revision_id, "granted_at": utc_now(), "released_at": None,
            "release_reason": None, "reason": reason,
        })
        return self.store.upsert(rec, actor=actor, event="ownership.acquired", inputs={"reason": reason},
                                 run_id=self.run_id)

    def check(self, token: str, resource: str, expected_base_revision_id: str | None) -> Record:
        rec = self.by_token(token)
        if rec is None:
            raise OwnershipError("unknown ownership token")
        if rec.state != "held":
            raise OwnershipError(f"ownership token was released ({rec.data.get('release_reason')}); "
                                 "stale holders cannot act after handoff")
        if rec.data.get("resource") != resource:
            raise OwnershipError(f"token is for resource {rec.data.get('resource')!r}, not {resource!r}")
        if rec.data.get("base_revision_id") != expected_base_revision_id:
            raise OwnershipError(f"base revision mismatch: token is bound to {rec.data.get('base_revision_id')}, "
                                 f"operation expects {expected_base_revision_id}")
        return rec

    def release(self, token: str, *, actor: str, reason: str) -> Record:
        rec = self.by_token(token)
        if rec is None:
            raise OwnershipError("unknown ownership token")
        if rec.state == "released":
            return rec
        rec.state = "released"
        rec.data["released_at"] = utc_now()
        rec.data["release_reason"] = reason
        return self.store.upsert(rec, actor=actor, event="ownership.released", inputs={"reason": reason},
                                 run_id=self.run_id)

    def advance(self, token: str, new_revision_id: str, *, actor: str, reason: str) -> Record:
        rec = self.by_token(token)
        if rec is None or rec.state != "held":
            raise OwnershipError("cannot advance a token that is unknown or released")
        previous = rec.data.get("base_revision_id")
        rec.data["base_revision_id"] = new_revision_id
        return self.store.upsert(rec, actor=actor, event="ownership.advanced",
                                 inputs={"reason": reason, "from": previous, "to": new_revision_id}, run_id=self.run_id)

    def handoff(self, token: str, to_holder: str, *, revision_id: str, changed_parts: list[str],
                assumptions: list[str], findings: list[str], pending_issues: list[str], actor: str,
                reason: str) -> tuple[Record, Record]:
        old = self.by_token(token)
        if old is None or old.state != "held":
            raise OwnershipError("handoff requires a held token")
        resource = old.data["resource"]
        handoff = Record.new("handoff", {
            "resource": resource, "from_holder": old.data["holder"], "to_holder": to_holder,
            "revision_id": revision_id, "changed_parts": list(changed_parts), "assumptions": list(assumptions),
            "findings": list(findings), "pending_issues": list(pending_issues), "reason": reason, "at": utc_now(),
        })
        self.release(token, actor=actor, reason=f"handoff to {to_holder}: {reason}")
        handoff = self.store.upsert(handoff, actor=actor, event="handoff.created", inputs={"reason": reason},
                                    run_id=self.run_id)
        new = self.acquire(resource, to_holder, revision_id, actor=actor,
                           reason=f"handoff from {old.data['holder']} ({handoff.id})")
        return handoff, new

    def history(self, resource: str) -> list[dict[str, Any]]:
        return [r.to_json() for r in self.store.list("ownership") if r.data.get("resource") == resource]
