"""Unit tests for the parts that must not be approximately right."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
import structlog

from app.core.money import (
    MICROS_PER_USD,
    cost_micros,
    format_usd,
    format_usd_precise,
    micros_to_usd,
    pct,
    usd_to_micros,
)
from app.core.security import (
    generate_api_key,
    hash_api_key,
    key_prefix,
    keys_match,
)
from app.core.tokenizer import count_prompt_tokens
from app.logging_setup import API_KEY_RE, redact_api_keys


# ------------------------------------------------------------------- money


def test_usd_to_micros_is_exact_for_awkward_decimals():
    # 0.1 has no exact binary representation; routing through str avoids it.
    assert usd_to_micros(0.1) == 100_000
    assert usd_to_micros("0.000001") == 1
    assert usd_to_micros(Decimal("50.00")) == 50 * MICROS_PER_USD
    assert usd_to_micros(0.07) == 70_000


def test_no_drift_accumulating_many_small_charges():
    """The failure mode this design exists to avoid.

    A float accumulator lands off by a visible amount over a hundred thousand
    charges; integer micro-dollars are exact by construction.
    """
    charge = usd_to_micros("0.000037")
    total = sum(charge for _ in range(100_000))
    assert total == 3_700_000
    assert micros_to_usd(total) == Decimal("3.7")

    naive = 0.0
    for _ in range(100_000):
        naive += 0.000037
    assert naive != 3.7  # the very drift the integer path avoids


def test_cost_micros_rounds_up():
    """Fractions round in the budget's favour, never the spender's."""
    # 1 token at 2500 µ$/1k = 2.5 µ$ → 3, not 2.
    assert cost_micros(1, 2_500) == 3
    assert cost_micros(1_000, 2_500) == 2_500
    assert cost_micros(0, 2_500) == 0
    assert cost_micros(400, 0) == 0


def test_formatting_distinguishes_totals_from_single_calls():
    assert format_usd(50 * MICROS_PER_USD) == "50.00"
    # A single call often costs a fraction of a cent; cent-rounding it to
    # "0.00" would report nothing useful.
    assert format_usd(1_838) == "0.00"
    assert format_usd_precise(1_838) == "0.001838"
    assert format_usd_precise(0) == "0.0"


def test_pct_handles_missing_limits():
    assert pct(50, 100) == 0.5
    assert pct(50, 0) == 0.0  # no limit configured → not "infinitely over"


# ---------------------------------------------------------------- security


def test_generated_keys_are_unique_and_prefixed():
    keys = {generate_api_key() for _ in range(500)}
    assert len(keys) == 500
    assert all(k.startswith("sk-agent-") for k in keys)
    assert all(len(k) > 40 for k in keys)


def test_hashing_is_deterministic_and_not_reversible():
    raw = generate_api_key()
    digest = hash_api_key(raw)

    assert hash_api_key(raw) == digest, "hash must be stable for lookup by value"
    assert len(digest) == 64
    assert raw not in digest
    assert digest != hash_api_key(generate_api_key())
    assert keys_match(raw, digest)
    assert not keys_match(generate_api_key(), digest)


def test_key_prefix_is_not_enough_to_authenticate():
    raw = generate_api_key()
    prefix = key_prefix(raw)
    assert raw.startswith(prefix)
    assert len(prefix) < len(raw) / 2
    assert not keys_match(prefix, hash_api_key(raw))


# --------------------------------------------------------------- redaction


def test_log_redaction_scrubs_keys_from_every_field():
    raw = generate_api_key()
    event = redact_api_keys(
        None,
        "info",
        {
            "event": f"authenticating {raw}",
            "headers": {"X-Agent-Key": raw},
            "list": [raw, "harmless"],
            "count": 3,
        },
    )
    flattened = repr(event)
    assert raw not in flattened
    assert "sk-agent-***REDACTED***" in flattened
    assert event["count"] == 3
    assert "harmless" in flattened


def test_redaction_pattern_does_not_match_ordinary_text():
    assert not API_KEY_RE.search("sk-agent-")  # too short to be a key
    assert not API_KEY_RE.search("the agent budget controller")
    assert API_KEY_RE.search("sk-agent-abcdefghijkl")


def test_redaction_is_wired_into_the_configured_logger(capsys):
    """The processor must be active in the real logging pipeline, not merely
    importable — a redactor that is never installed protects nothing."""
    from app.logging_setup import configure_logging

    configure_logging("INFO", json_output=True)
    raw = generate_api_key()
    structlog.get_logger("test").info("agent authenticated", api_key=raw)

    captured = capsys.readouterr()
    assert raw not in captured.out
    assert "REDACTED" in captured.out
    logging.getLogger().handlers.clear()


# --------------------------------------------------------------- tokenizer


def test_token_estimate_scales_with_content():
    short = count_prompt_tokens([{"role": "user", "content": "hi"}])
    long = count_prompt_tokens([{"role": "user", "content": "word " * 500}])
    assert long > short * 10


def test_token_estimate_handles_multimodal_blocks():
    tokens = count_prompt_tokens(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                ],
            }
        ]
    )
    assert tokens > 0


@pytest.mark.parametrize("messages", [[], [{"role": "user"}]])
def test_token_estimate_never_returns_zero(messages):
    """A zero estimate would reserve nothing and let a malformed request run
    free of the budget."""
    assert count_prompt_tokens(messages) >= 1
