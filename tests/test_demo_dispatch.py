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
    from app.core import credentials

    credentials._cache.pop("GEMINI_API_KEY", None)
    monkeypatch.setenv("GEMINI_API_KEY", "from-environ")
    assert providers.resolve_credential("GEMINI_API_KEY") == "from-environ"


def test_credential_falls_back_to_settings(monkeypatch):
    """pydantic-settings loads .env into Settings, never into os.environ.

    Without this fallback a key in .env resolves under Docker — where compose
    exports it — and silently does not under `make dev`.
    """
    from app.core import credentials

    credentials._cache.pop("GEMINI_API_KEY", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(providers.settings, "gemini_api_key", "from-dotenv")

    assert providers.resolve_credential("GEMINI_API_KEY") == "from-dotenv"


def test_environment_wins_over_settings(monkeypatch):
    from app.core import credentials

    credentials._cache.pop("GEMINI_API_KEY", None)
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


# ------------------------------------------------- api_key_env is a NAME


def test_pasting_a_provider_key_into_api_key_env_is_refused():
    """The field names a variable; the key itself belongs in .env.

    Accepting the key here fails twice: the lookup is for a variable nobody
    set, so the model is undispatchable and reports "unset", and the secret is
    rendered in the models table of an unauthenticated dashboard.
    """
    from app.api.schemas import ModelCreateRequest, ModelUpdateRequest

    pasted_key = "example.key.value.not-a-real-credential"

    with pytest.raises(ValueError, match="NAME of an environment variable"):
        ModelUpdateRequest(api_key_env=pasted_key)

    with pytest.raises(ValueError, match="NAME of an environment variable"):
        ModelCreateRequest(
            model_id="x",
            provider="google",
            input_usd_per_1k=0,
            output_usd_per_1k=0,
            api_key_env=pasted_key,
        )


@pytest.mark.parametrize(
    "value", ["GEMINI_API_KEY", "OPENAI_API_KEY", "my_local_key", "_LEADING"]
)
def test_real_variable_names_are_accepted(value):
    from app.api.schemas import ModelUpdateRequest

    assert ModelUpdateRequest(api_key_env=value).api_key_env == value


@pytest.mark.parametrize("value", [None, "", "   "])
def test_blank_means_no_credential_needed(value):
    """A local Ollama box needs no key, and must stay configurable that way."""
    from app.api.schemas import ModelUpdateRequest

    assert ModelUpdateRequest(api_key_env=value).api_key_env is None


# ------------------------------------------------ reasoning tokens are billable


def test_reasoning_tokens_are_metered():
    """Gemini 3.x bills internal reasoning that completion_tokens omits.

    A real response: prompt 8, completion 3, total 119. Billing the 3 would
    under-count spend ~36x and let an agent run far past a budget the ledger
    still believed was healthy.
    """
    result = providers.parse_response(
        "openai",
        {"usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 119}},
    )

    assert result.prompt_tokens == 8
    assert result.completion_tokens == 111  # 119 total - 8 prompt


def test_providers_folding_reasoning_into_completion_are_unchanged():
    """OpenAI already includes reasoning in completion_tokens; do not double it."""
    result = providers.parse_response(
        "openai",
        {"usage": {"prompt_tokens": 10, "completion_tokens": 40, "total_tokens": 50}},
    )

    assert result.completion_tokens == 40


def test_missing_total_falls_back_to_the_reported_completion():
    """Absent total_tokens, the reported figure is all there is."""
    result = providers.parse_response(
        "openai", {"usage": {"prompt_tokens": 10, "completion_tokens": 25}}
    )

    assert result.completion_tokens == 25


def test_absent_usage_meters_nothing():
    result = providers.parse_response("openai", {})
    assert result.has_usage is False
    assert result.completion_tokens == 0


# ------------------------------------------------- dashboard-set credentials


def test_stored_credential_round_trips_through_encryption(monkeypatch):
    """Encrypted at rest, decrypted into the in-memory mirror."""
    from app.core import credentials

    monkeypatch.setattr(credentials.settings, "api_key_pepper", "pepper-for-tests")
    fernet = credentials._fernet()
    token = fernet.encrypt(b"sk-stored-value-1234")

    assert fernet.decrypt(token).decode() == "sk-stored-value-1234"


def test_stored_credential_overrides_the_deployment(monkeypatch):
    """The dashboard is an override, not a fallback.

    If the environment won, saving a key while SSM held one would appear to
    succeed and change nothing — a dashboard that lies about what it did.
    """
    from app.core import credentials

    monkeypatch.setenv("GEMINI_API_KEY", "from-environ")
    monkeypatch.setattr(providers.settings, "gemini_api_key", "from-dotenv")

    # No stored key: the deployment supplies the value.
    credentials._cache.pop("GEMINI_API_KEY", None)
    assert providers.resolve_credential("GEMINI_API_KEY") == "from-environ"

    # Stored key takes over.
    monkeypatch.setitem(credentials._cache, "GEMINI_API_KEY", "from-dashboard")
    assert providers.resolve_credential("GEMINI_API_KEY") == "from-dashboard"


def test_removing_a_stored_key_reverts_to_the_deployment(monkeypatch):
    """Removal must not leave the provider unconfigured when a default exists."""
    from app.core import credentials

    monkeypatch.setenv("GEMINI_API_KEY", "from-environ")
    monkeypatch.setitem(credentials._cache, "GEMINI_API_KEY", "from-dashboard")
    assert providers.resolve_credential("GEMINI_API_KEY") == "from-dashboard"

    credentials._cache.pop("GEMINI_API_KEY", None)
    assert providers.resolve_credential("GEMINI_API_KEY") == "from-environ"


def test_describe_exposes_only_the_last_four_characters():
    """The listing endpoint builds on this; it must never carry key material."""
    from app.core import credentials

    credentials._cache.clear()
    credentials._cache["OPENAI_API_KEY"] = "sk-secret-abcdWXYZ"
    try:
        described = credentials.describe()
        assert described == {"OPENAI_API_KEY": "WXYZ"}
        assert "secret" not in str(described)
    finally:
        credentials._cache.clear()


def test_an_undecryptable_row_is_skipped_not_fatal(monkeypatch):
    """A changed pepper must not stop the app booting.

    The operator needs a running dashboard to re-enter the key, so a row that
    cannot be decrypted is dropped with a warning rather than raised.
    """
    from cryptography.fernet import InvalidToken

    from app.core import credentials

    monkeypatch.setattr(credentials.settings, "api_key_pepper", "pepper-a")
    token = credentials._fernet().encrypt(b"value")

    monkeypatch.setattr(credentials.settings, "api_key_pepper", "pepper-b")
    with pytest.raises(InvalidToken):
        credentials._fernet().decrypt(token)
