"""Roles and assignment rules (design Section 14; addendum D10, R-107, R-108; base R-8, R-11, R-12, R-74, R-88).

A *seat* is a provider binding (``A``, ``B``, and later the image seat ``I``). A *role* is a duty assigned per
task by this module, in code, with a one-line rationale. Roles are never permanent (R-8). The one rule that
no override can break: the seat that made an operation never reviews, verifies, or reassesses it (R-107).
"""
from __future__ import annotations

from dataclasses import dataclass

ROLES = ("observer", "planner", "builder", "corrector", "reviewer", "verifier", "reassessor", "art_director",
         "image_generator")
LLM_ROLES = tuple(r for r in ROLES if r != "image_generator")
INDEPENDENT_OF_AUTHOR = ("reviewer", "verifier", "reassessor")
ART_DIRECTOR_ROTATION_AFTER = 2     # rejected generation rounds before the art director rotates (R-108)


class RoleConflict(Exception):
    pass


@dataclass(frozen=True)
class Assignment:
    role: str
    seat: str
    rationale: str


def other_seat(seats: list[str], seat: str) -> str:
    if len(seats) < 2:
        raise ValueError("two LLM seats are required (R-7)")
    for s in seats:
        if s != seat:
            return s
    raise ValueError(f"no seat other than {seat!r} among {seats}")


def assert_no_conflict(role: str, seat: str, author: str | None) -> None:
    """R-107: a seat never holds a role that judges its own operation."""
    if role in INDEPENDENT_OF_AUTHOR and author is not None and seat == author:
        raise RoleConflict(f"seat {seat} made the operation under {role} and may not hold that role for it (R-107)")


def assign(role: str, *, seats: list[str], overrides: dict[str, str] | None = None, author: str | None = None,
           build_count: int = 0, proposer: str | None = None, reassigned_to: str | None = None,
           rejected_rounds: int = 0, image_seat: str | None = None) -> Assignment:
    """Decide the seat for ``role`` on one task.

    ``author``: the seat whose operation is being reviewed/verified/reassessed (required for those roles).
    ``build_count``: build tasks so far (alternation). ``proposer``/``reassigned_to``: for the corrector.
    ``rejected_rounds``: rejected concept-generation rounds (art director rotation). ``image_seat``: the
    image-generation seat, the only holder of ``image_generator``.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
    if role in INDEPENDENT_OF_AUTHOR and author is None:
        raise ValueError(f"role {role} needs the operation's author so the independence rule can be enforced (R-107)")
    override = (overrides or {}).get(role)
    if override is not None and (override in seats or override == image_seat):
        assert_no_conflict(role, override, author)
        if role == "image_generator" and override != image_seat:
            raise RoleConflict("only the image seat may generate images (D10)")
        return Assignment(role, override, "user override")
    if role == "image_generator":
        if not image_seat:
            raise RoleConflict("no image seat is configured; LLM seats never hold image_generator (D10)")
        return Assignment(role, image_seat, "the image seat is a tool: it generates and judges nothing (D10)")
    if role == "planner":
        return Assignment(role, seats[0], "first agent by configuration order; both agents' independent observations are included")
    if role == "builder":
        seat = seats[build_count % len(seats)]
        return Assignment(role, seat, f"alternating build ownership (build task #{build_count + 1})")
    if role == "corrector":
        if reassigned_to:
            return Assignment(role, reassigned_to, "reassigned after reassessment by the other seat (R-88)")
        if proposer is None:
            raise ValueError("corrector needs the finding's proposer")
        return Assignment(role, proposer, "the seat that proposed the correction implements it (R-7, R-56)")
    if role == "reviewer":
        return Assignment(role, other_seat(seats, author), "reviewer is the non-owner; independent evidence pass (R-8, R-11)")
    if role == "verifier":
        return Assignment(role, other_seat(seats, author), "the non-corrector verifies on renders (R-74)")
    if role == "reassessor":
        return Assignment(role, other_seat(seats, author), "fresh analysis by the agent that did not make the failed corrections (R-88)")
    if role == "art_director":
        if rejected_rounds >= ART_DIRECTOR_ROTATION_AFTER:
            return Assignment(role, other_seat(seats, seats[0]), f"rotated after two rejected generation rounds (R-108)")
        return Assignment(role, seats[0], "art director defaults to seat A so the canon description keeps one voice (R-108)")
    if role == "observer":
        raise ValueError("observer is held by every LLM seat independently; there is nothing to assign (R-11)")
    raise ValueError(f"no rule for role {role!r}")   # pragma: no cover
