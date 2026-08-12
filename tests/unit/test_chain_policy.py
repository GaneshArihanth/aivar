"""Fallback chain validation and runtime filtering."""

from __future__ import annotations

import pytest

from app.core.policy import ChainInvalid, usable_chain, validate_chain
from app.core.pricing import ModelPrice, pricing

CATALOG = [
    ModelPrice("gpt-4o", "openai", "GPT-4o", 2_500, 10_000, 60),
    ModelPrice("gpt-4o-mini", "openai", "GPT-4o mini", 150, 600, 20),
    ModelPrice("gpt-4.1-nano", "openai", "GPT-4.1 nano", 100, 400, 10),
    ModelPrice("claude-sonnet-4", "anthropic", "Claude Sonnet 4", 3_000, 15_000, 40),
    ModelPrice("claude-haiku-4-5", "anthropic", "Claude Haiku 4.5", 800, 4_000, 15),
    ModelPrice("gemini-flash", "google", "Gemini Flash", 100, 400, 15),
    ModelPrice("retired", "openai", "Retired", 50, 50, 5, is_active=False),
]


@pytest.fixture(autouse=True)
def catalog():
    pricing.replace(CATALOG)
    yield
    pricing.replace([])


def check(chain, *, preferred=None, cross=False):
    return validate_chain(
        chain, preferred_model=preferred or chain[0], allow_cross_provider=cross
    )


# ------------------------------------------------------------------ accepted


def test_descending_same_provider_chain_is_clean():
    report = check(["gpt-4o", "gpt-4o-mini", "gpt-4.1-nano"])
    assert report.warnings == []
    assert report.crosses_providers is False
    assert [s["model_id"] for s in report.steps] == [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1-nano",
    ]


def test_cross_provider_chain_is_allowed_with_permission_and_flagged():
    report = check(
        ["gpt-4o", "claude-haiku-4-5", "gemini-flash"], cross=True
    )
    assert report.crosses_providers is True
    assert any("crosses providers" in w for w in report.warnings)


def test_a_pricier_step_warns_rather_than_refusing():
    """Dead weight for budget pressure, but it becomes the head if the step
    above it is removed from the catalog — the operator's call, not ours."""
    report = check(["gpt-4o", "claude-sonnet-4", "gpt-4o-mini"], cross=True)
    assert any("costs more than the step above" in w for w in report.warnings)
    assert report.chain[1] == "claude-sonnet-4"


def test_single_entry_chain_warns_that_nothing_can_catch_it():
    report = check(["gpt-4o"])
    assert any("nothing to fall back to" in w for w in report.warnings)


# ------------------------------------------------------------------ refused


def test_cross_provider_without_permission_is_refused():
    with pytest.raises(ChainInvalid) as exc:
        check(["gpt-4o", "claude-haiku-4-5"], cross=False)
    assert exc.value.position == 1
    assert "cross-provider" in exc.value.message


def test_duplicate_entries_are_refused():
    with pytest.raises(ChainInvalid) as exc:
        check(["gpt-4o", "gpt-4o-mini", "gpt-4o-mini"])
    assert exc.value.position == 2


def test_unknown_model_is_refused():
    with pytest.raises(ChainInvalid) as exc:
        check(["gpt-4o", "nonexistent"])
    assert "not in the model catalog" in exc.value.message


def test_inactive_model_is_refused():
    """It could never serve a request, so it must not sit in a ladder."""
    with pytest.raises(ChainInvalid) as exc:
        check(["gpt-4o", "retired"])
    assert "inactive" in exc.value.message


def test_chain_must_start_with_the_preferred_model():
    with pytest.raises(ChainInvalid) as exc:
        check(["gpt-4o-mini", "gpt-4.1-nano"], preferred="gpt-4o")
    assert exc.value.position == 0


def test_empty_chain_is_refused():
    with pytest.raises(ChainInvalid):
        validate_chain([], preferred_model="gpt-4o", allow_cross_provider=False)


# ------------------------------------------------- runtime filtering


def test_usable_chain_drops_entries_that_can_no_longer_serve():
    """A chain is written once and read for months. Between those moments a
    model can be deactivated or permission withdrawn."""
    chain = ["gpt-4o", "retired", "claude-haiku-4-5", "gpt-4.1-nano"]

    same_provider_only = usable_chain(
        chain, allow_cross_provider=False, allow_substitution=True
    )
    assert same_provider_only == ["gpt-4o", "gpt-4.1-nano"], (
        "inactive and cross-provider entries should both be filtered out"
    )

    crossing = usable_chain(chain, allow_cross_provider=True, allow_substitution=True)
    assert crossing == ["gpt-4o", "claude-haiku-4-5", "gpt-4.1-nano"]


def test_usable_chain_collapses_to_the_head_when_substitution_is_off():
    chain = ["gpt-4o", "gpt-4o-mini"]
    assert usable_chain(
        chain, allow_cross_provider=False, allow_substitution=False
    ) == ["gpt-4o"]


def test_cheaper_alternatives_respect_the_provider_boundary():
    same = pricing.cheaper_alternatives("gpt-4o")
    assert {m.provider for m in same} == {"openai"}

    crossed = pricing.cheaper_alternatives("gpt-4o", cross_provider=True)
    assert {m.provider for m in crossed} >= {"openai", "anthropic", "google"}
    # Ordered richest-first, so the ladder steps down gradually.
    costs = [m.blended_micros_per_1k() for m in crossed]
    assert costs == sorted(costs, reverse=True)
    # Inactive models never appear as an alternative.
    assert all(m.model_id != "retired" for m in crossed)
