"""Model catalog administration against the live stack."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def custom_model(api):
    """A throwaway catalog entry, removed afterwards."""
    model_id = f"test-model-{uuid.uuid4().hex[:8]}"
    created = []

    async def _make(**overrides):
        payload = {
            "model_id": model_id,
            "provider": "ollama",
            "display_name": "Test Local Model",
            "input_usd_per_1k": 0.0002,
            "output_usd_per_1k": 0.0008,
            "tier_rank": 12,
            "provider_kind": "openai",
            "base_url": "http://localhost:19999/v1",
            **overrides,
        }
        response = await api.post("/admin/models", json=payload)
        response.raise_for_status()
        created.append(payload["model_id"])
        return response.json()

    yield _make

    for mid in created:
        await api.delete(f"/admin/models/{mid}")


@pytest.mark.asyncio
async def test_custom_model_can_be_registered_and_priced(api, custom_model):
    model = await custom_model()
    assert model["is_custom"] is True
    assert model["input_usd_per_1k"] == pytest.approx(0.0002)
    assert model["output_usd_per_1k"] == pytest.approx(0.0008)
    assert model["dispatchable"] is True

    listed = (await api.get("/admin/models")).json()
    assert any(m["model_id"] == model["model_id"] for m in listed)


@pytest.mark.asyncio
async def test_api_keys_are_referenced_by_env_name_never_stored(api, custom_model):
    """The catalog holds the *name* of the variable, never a secret.

    An operator must be able to see which key a model needs without the app
    ever being able to show, store or leak the value.
    """
    model = await custom_model(api_key_env="TEST_PROVIDER_KEY_NAME")
    assert model["api_key_env"] == "TEST_PROVIDER_KEY_NAME"
    # Not set in this process, so the UI shows it as missing rather than
    # pretending the model is ready to dispatch.
    assert model["credential_present"] is False

    body = (await api.get("/admin/models")).text
    assert "TEST_PROVIDER_KEY_NAME" in body, "the variable name should be visible"
    for secretish in ("sk-", "secret", "password", "token="):
        assert secretish not in body.lower().replace("sk-agent", ""), (
            f"catalog response contains something resembling a credential: {secretish}"
        )


@pytest.mark.asyncio
async def test_pricing_change_takes_effect_without_a_restart(api, custom_model):
    """Editing a price edits what reservations are sized against, so it has to
    reach the in-process pricing mirror immediately."""
    model = await custom_model()
    await api.patch(
        f"/admin/models/{model['model_id']}",
        json={"input_usd_per_1k": 0.5, "output_usd_per_1k": 1.5},
    )
    refreshed = next(
        m
        for m in (await api.get("/admin/models")).json()
        if m["model_id"] == model["model_id"]
    )
    assert refreshed["input_usd_per_1k"] == pytest.approx(0.5)

    # The probe resolves through the pricing cache; reaching the endpoint at
    # all proves the new entry is live in-process, not just in PostgreSQL.
    probe = (await api.post(f"/admin/models/{model['model_id']}/test")).json()
    assert probe["base_url"] == "http://localhost:19999/v1"


@pytest.mark.asyncio
async def test_probe_reports_an_unreachable_endpoint_rather_than_success(
    api, custom_model
):
    """A "Test" button that answers about the mock while the operator is asking
    about their own box would report success for a machine that is switched
    off. It must contact the model's own endpoint."""
    model = await custom_model()
    result = (await api.post(f"/admin/models/{model['model_id']}/test")).json()

    assert result["ok"] is False
    assert result["stage"] == "connect"
    assert result["tests_own_endpoint"] is True
    assert "19999" in result["detail"]


@pytest.mark.asyncio
async def test_model_in_use_cannot_be_deleted(api, custom_model, make_agent):
    model = await custom_model()
    agent = await make_agent(monthly_usd=5, session_usd=1, model=model["model_id"])

    response = await api.delete(f"/admin/models/{model['model_id']}")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["type"] == "model_in_use"
    assert agent.name in error["agents"]


@pytest.mark.asyncio
async def test_unknown_provider_kind_is_rejected(api):
    response = await api.post(
        "/admin/models",
        json={
            "model_id": "bad-kind-model",
            "provider": "x",
            "input_usd_per_1k": 0,
            "output_usd_per_1k": 0,
            "provider_kind": "telepathy",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_model_id_is_rejected(api, custom_model):
    model = await custom_model()
    response = await api.post(
        "/admin/models",
        json={
            "model_id": model["model_id"],
            "provider": "ollama",
            "input_usd_per_1k": 0,
            "output_usd_per_1k": 0,
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "model_exists"


@pytest.mark.asyncio
async def test_seeded_catalog_carries_real_endpoints(api):
    """Item 2: models are registered with the endpoint they actually live at."""
    models = {m["model_id"]: m for m in (await api.get("/admin/models")).json()}

    assert models["gpt-4o"]["base_url"] == "https://api.openai.com/v1"
    assert models["gpt-4o"]["provider_kind"] == "openai"
    assert models["claude-sonnet-4"]["base_url"] == "https://api.anthropic.com/v1"
    assert models["claude-sonnet-4"]["provider_kind"] == "anthropic"
    # Gemini is reached through its OpenAI-compatibility endpoint, so it needs
    # no adapter of its own.
    assert models["gemini-3.5-flash"]["provider_kind"] == "openai"
    assert "generativelanguage" in models["gemini-3.5-flash"]["base_url"]
    # A local runtime, free at the point of use.
    assert models["llama3.1:8b"]["input_usd_per_1k"] == 0
