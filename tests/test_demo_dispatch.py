"""Live-dispatch opt-in and credential resolution for the Demo page.

Two behaviours are load-bearing and easy to regress silently:

* the caller must not be able to spend real money by sending a header, and
* a provider key written to .env must resolve, because that is where every
  document tells the operator to put it.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.core import providers, upstream
from app.core.pricing import ModelPrice


def _model(**overrides) -> ModelPrice:
    """Gemini as the catalog actually records it. Prices are micro-dollars per
    1k tokens — $0.0001 is 100µ$ — matching the integer money used throughout."""
    base = dict(
        model_id="gemini-2.0-flash",
        provider="google",
        display_name="Gemini 2.0 Flash",
        input_micros_per_1k=100,
        output_micros_per_1k=400,
        tier_rank=15,
        provider_kind="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
    )
    base.update(overrides)
    return ModelPrice(**base)


# ------------------------------------------------------------------- routing


def test_mock_mode_ignores_the_catalog_endpoint(monkeypatch):
    """Default behaviour: nothing reaches a real provider."""
    monkeypatch.setattr(upstream.settings, "upstream_mode", "mock")
    monkeypatch.setattr(upstream.settings, "upstream_base_url", "http://mock/v1")

    base_url, kind, credential = upstream.resolve_route(_model())

    assert base_url == "http://mock/v1"
    assert kind == "openai"
    assert credential is None


def test_force_model_endpoint_reaches_the_real_provider(monkeypatch):
    """The Demo page's opt-in must actually change where the call goes."""
    monkeypatch.setattr(upstream.settings, "upstream_mode", "mock")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    base_url, kind, credential = upstream.resolve_route(
        _model(), force_model_endpoint=True
    )

    assert base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert kind == "openai"
    assert credential == "test-key"


def test_forced_route_without_a_base_url_falls_back_to_the_mock(monkeypatch):
    """A model with nothing of its own cannot be forced anywhere."""
    monkeypatch.setattr(upstream.settings, "upstream_mode", "mock")
    monkeypatch.setattr(upstream.settings, "upstream_base_url", "http://mock/v1")

    base_url, _, _ = upstream.resolve_route(
        _model(base_url=None), force_model_endpoint=True
    )

    assert base_url == "http://mock/v1"


# --------------------------------------------------------------- credentials


def test_credential_resolves_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-environ")
    assert providers.resolve_credential("GEMINI_API_KEY") == "from-environ"


def test_credential_falls_back_to_settings(monkeypatch):
    """pydantic-settings loads .env into Settings, never into os.environ.

    Without this fallback a key in .env resolves under Docker — where compose
    exports it — and silently does not under `make dev`.
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(providers.settings, "gemini_api_key", "from-dotenv")

    assert providers.resolve_credential("GEMINI_API_KEY") == "from-dotenv"


def test_environment_wins_over_settings(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-environ")
    monkeypatch.setattr(providers.settings, "gemini_api_key", "from-dotenv")

    assert providers.resolve_credential("GEMINI_API_KEY") == "from-environ"


def test_unknown_variable_name_resolves_to_nothing(monkeypatch):
    """A catalog entry naming a variable nobody set must not silently pass."""
    monkeypatch.delenv("NOPE_API_KEY", raising=False)
    assert providers.resolve_credential("NOPE_API_KEY") is None
    assert providers.resolve_credential(None) is None


def test_missing_credential_is_refused_rather_than_sent_unauthenticated(monkeypatch):
    monkeypatch.setattr(upstream.settings, "upstream_mode", "live")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(upstream.settings, "gemini_api_key", "")
    monkeypatch.setattr(providers.settings, "gemini_api_key", "")

    with pytest.raises(upstream.UpstreamNotConfigured) as exc:
        upstream.resolve_route(_model())

    assert "GEMINI_API_KEY" in str(exc.value)


# -------------------------------------------------------------- the gate


def test_demo_allow_live_defaults_to_enabled():
    """Deliberately on: this deployment is public by the operator's choice."""
    assert Settings(_env_file=None).demo_allow_live is True


def test_demo_allow_live_can_be_switched_off():
    """The kill switch has to work, since it is the only brake on real spend."""
    assert Settings(_env_file=None, demo_allow_live=False).demo_allow_live is False
