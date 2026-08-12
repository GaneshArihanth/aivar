"""Provider adapter translation.

These are the functions that make a cross-provider fallback chain possible: a
chain stepping from GPT-4o to Claude has to change request shape, auth header
and usage field names mid-flight, and the caller must never see it. Getting the
usage fields wrong would be quietly expensive — the settle step would charge
zero for every Anthropic call.
"""

from __future__ import annotations

import pytest

from app.core import providers


# ------------------------------------------------------------------ openai


def test_openai_request_is_passed_through_with_the_served_model():
    call = providers.build_request(
        "openai",
        "gpt-4o-mini",
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100},
        "sk-test",
    )
    assert call.path == "/chat/completions"
    assert call.headers["Authorization"] == "Bearer sk-test"
    # The substituted model must be what actually goes on the wire.
    assert call.payload["model"] == "gpt-4o-mini"
    assert call.payload["max_tokens"] == 100


def test_openai_request_omits_auth_when_no_credential():
    """Local runtimes (Ollama, vLLM) usually need no key at all."""
    call = providers.build_request("openai", "llama3.1:8b", {"messages": []}, None)
    assert "Authorization" not in call.headers


def test_openai_usage_is_read_from_the_response():
    result = providers.parse_response(
        "openai",
        {"usage": {"prompt_tokens": 12, "completion_tokens": 34}, "choices": []},
    )
    assert (result.prompt_tokens, result.completion_tokens) == (12, 34)
    assert result.has_usage


def test_missing_usage_is_reported_so_the_caller_can_charge_the_estimate():
    result = providers.parse_response("openai", {"choices": []})
    assert not result.has_usage
    assert result.prompt_tokens == 0


# --------------------------------------------------------------- anthropic


def test_anthropic_lifts_the_system_prompt_out_of_messages():
    """Anthropic takes `system` as a top-level field, not a message role.

    Left in the messages array it is rejected outright, so a chain that fell
    back to Claude would fail on exactly the requests that carry a system
    prompt — which is most of them.
    """
    call = providers.build_request(
        "anthropic",
        "claude-sonnet-4",
        {
            "messages": [
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "hi"},
            ],
            "max_tokens": 256,
        },
        "sk-ant-test",
    )
    assert call.path == "/messages"
    assert call.headers["x-api-key"] == "sk-ant-test"
    assert "Authorization" not in call.headers
    assert call.headers["anthropic-version"]
    assert call.payload["system"] == "Be terse."
    assert [m["role"] for m in call.payload["messages"]] == ["user"]


def test_anthropic_always_sends_max_tokens():
    """Required by Anthropic, optional for OpenAI. The proxy always has a value
    because the reservation was sized against it."""
    call = providers.build_request(
        "anthropic", "claude-sonnet-4", {"messages": [{"role": "user", "content": "x"}]}, None
    )
    assert call.payload["max_tokens"] > 0


def test_anthropic_response_is_normalised_to_the_openai_shape():
    """A client pointed at this proxy must not have to care which provider
    answered — including when substitution crossed a boundary mid-chain."""
    result = providers.parse_response(
        "anthropic",
        {
            "id": "msg_1",
            "model": "claude-sonnet-4",
            "content": [{"type": "text", "text": "hello there"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 9, "output_tokens": 21},
        },
    )
    assert result.prompt_tokens == 9
    assert result.completion_tokens == 21
    assert result.has_usage

    body = result.body
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "hello there"}
    assert body["usage"]["total_tokens"] == 30


def test_anthropic_multi_block_content_is_joined():
    result = providers.parse_response(
        "anthropic",
        {
            "content": [
                {"type": "text", "text": "one "},
                {"type": "thinking", "thinking": "ignored"},
                {"type": "text", "text": "two"},
            ],
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
    )
    assert result.body["choices"][0]["message"]["content"] == "one two"


# ------------------------------------------------------------- dispatchable


@pytest.mark.parametrize(
    "kind,expected", [("openai", True), ("anthropic", True), ("bedrock", False), ("vertex", False)]
)
def test_only_implemented_wire_formats_are_dispatchable(kind, expected):
    """Bedrock and Vertex need request signing rather than a bearer token.
    They are registerable for pricing and policy, but must not be dispatched
    as though a key would work."""
    assert providers.is_dispatchable(kind) is expected
    assert kind in providers.PROVIDER_KINDS


def test_credentials_are_read_from_the_environment_by_name(monkeypatch):
    monkeypatch.setenv("SOME_PROVIDER_KEY", "secret-value")
    assert providers.resolve_credential("SOME_PROVIDER_KEY") == "secret-value"
    assert providers.resolve_credential("NOT_SET_ANYWHERE") is None
    assert providers.resolve_credential(None) is None
