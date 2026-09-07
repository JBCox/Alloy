"""Roles and assignment rules (design Section 14, addendum R-107, R-108; base R-8, R-11, R-74, R-88).
The seat that made an operation never reviews or verifies it, even under a user override."""
from __future__ import annotations

import pytest

from builder.roles import LLM_ROLES, ROLES, RoleConflict, assign, other_seat

SEATS = ["A", "B"]


def test_role_table_lists_every_duty_and_the_image_seat_holds_only_generation():
    for r in ("observer", "planner", "builder", "corrector", "reviewer", "verifier", "reassessor", "art_director",
              "image_generator"):
        assert r in ROLES
    assert "image_generator" not in LLM_ROLES and "reviewer" in LLM_ROLES


def test_builder_alternates_and_records_rationale():
    a = assign("builder", seats=SEATS, build_count=0)
    b = assign("builder", seats=SEATS, build_count=1)
    c = assign("builder", seats=SEATS, build_count=2)
    assert (a.seat, b.seat, c.seat) == ("A", "B", "A")
    assert "alternating" in a.rationale and "#1" in a.rationale and "#3" in c.rationale


def test_planner_is_first_seat_by_configuration_order():
    p = assign("planner", seats=SEATS)
    assert p.seat == "A" and "configuration order" in p.rationale


def test_reviewer_verifier_reassessor_are_never_the_author():
    assert assign("reviewer", seats=SEATS, author="A").seat == "B"
    assert assign("reviewer", seats=SEATS, author="B").seat == "A"
    v = assign("verifier", seats=SEATS, author="B")
    assert v.seat == "A" and "R-74" in v.rationale
    r = assign("reassessor", seats=SEATS, author="B")
    assert r.seat == "A" and "R-88" in r.rationale
    with pytest.raises(RoleConflict):
        assign("reviewer", seats=SEATS, author="A", overrides={"reviewer": "A"})
    with pytest.raises(RoleConflict):
        assign("verifier", seats=SEATS, author="B", overrides={"verifier": "B"})
    with pytest.raises(ValueError):
        assign("reviewer", seats=SEATS)  # an author is required to guarantee the rule


def test_user_override_wins_when_it_does_not_conflict():
    o = assign("builder", seats=SEATS, build_count=0, overrides={"builder": "B"})
    assert o.seat == "B" and o.rationale == "user override"
    assert assign("builder", seats=SEATS, build_count=0, overrides={"builder": "Z"}).seat == "A"  # unknown seat ignored


def test_corrector_is_the_proposer_unless_reassigned():
    assert assign("corrector", seats=SEATS, proposer="B").seat == "B"
    c = assign("corrector", seats=SEATS, proposer="B", reassigned_to="A")
    assert c.seat == "A" and "reassign" in c.rationale


def test_art_director_defaults_to_a_and_rotates_after_two_rejected_rounds():
    assert assign("art_director", seats=SEATS).seat == "A"
    assert assign("art_director", seats=SEATS, rejected_rounds=1).seat == "A"
    rotated = assign("art_director", seats=SEATS, rejected_rounds=2)
    assert rotated.seat == "B" and "two rejected" in rotated.rationale
    assert assign("art_director", seats=SEATS, overrides={"art_director": "B"}).seat == "B"


def test_image_generator_only_goes_to_the_image_seat():
    assert assign("image_generator", seats=SEATS, image_seat="I").seat == "I"
    with pytest.raises(RoleConflict):
        assign("image_generator", seats=SEATS)


def test_other_seat_requires_two_seats():
    assert other_seat(SEATS, "A") == "B"
    with pytest.raises(ValueError):
        other_seat(["A"], "A")
