"""Provider API keys, set from the dashboard.

A deployed instance has no shell for most operators: keys live in SSM, and
changing one otherwise means editing a parameter and running a redeploy. This
router lets a key be submitted from the web page, which on AWS is the only
route that does not require console access.

Its own module, and its own /admin/credentials prefix, rather than living under
the model catalog: a GET on /admin/models/{model_id} would happily match
"credentials" and shadow the listing.

The value is write-only throughout. Nothing here returns it and nothing logs
it — only the last four characters, enough to tell two keys apart and not
enough to reconstruct one. That matters more than usual, because this dashboard
has no authentication in front of it.
"""

from __future__ import annotations

import os

import structlog
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import http_error
from app.config import settings
from app.core import credentials, providers
from app.db.models import ModelCatalog
from app.db.session import get_session

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/credentials", tags=["admin:credentials"])

class CredentialRequest(BaseModel):
    value: str = Field(min_length=8, max_length=500)

    @field_validator("value")
    @classmethod
    def _strip(cls, v: str) -> str:
        # Pasted keys routinely carry whitespace or a stray newline, which would
        # otherwise be sent in the Authorization header and rejected upstream as
        # a malformed credential.
        stripped = v.strip()
        if not stripped:
            raise ValueError("Key cannot be blank")
        return stripped


@router.get("")
async def list_credentials(session: AsyncSession = Depends(get_session)) -> dict:
    """Which provider keys are configured, and where each one comes from.

    Every row is editable. A stored key overrides whatever the deployment
    supplies, and removing it falls back to that default rather than leaving
    the provider unconfigured — so the environment is a default, not a lock.

    The response distinguishes the two so the UI can say which value is
    actually in use, and offer "revert to the deployed key" rather than a bare
    delete when there is something to revert to.
    """
    rows = list(
        (
            await session.execute(
                select(ModelCatalog).where(ModelCatalog.is_active.is_(True))
            )
        ).scalars()
    )
    names = sorted({row.api_key_env for row in rows if row.api_key_env})
    stored = credentials.describe()

    out = []
    for name in names:
        has_env_default = bool(
            os.environ.get(name) or getattr(settings, name.lower(), "")
        )
        is_stored = name in stored
        out.append(
            {
                "env_name": name,
                "is_set": bool(providers.resolve_credential(name)),
                # Which value the proxy will actually send.
                "source": "stored" if is_stored else ("environment" if has_env_default else None),
                "last4": stored.get(name),
                # There is a deployed value underneath, so removing the stored
                # key reverts rather than unconfiguring.
                "has_env_default": has_env_default,
                "overrides_environment": is_stored and has_env_default,
                "editable": True,
            }
        )
    return {"credentials": out}


@router.put("/{env_name}")
async def set_credential(
    env_name: str,
    payload: CredentialRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await credentials.put(session, env_name, payload.value)
    await session.commit()
    # Deliberately no key material, and no event payload carrying one.
    log.info("credential.set", env_name=env_name, last4=row.last4)
    return {"env_name": env_name, "last4": row.last4, "is_set": True}


@router.delete("/{env_name}")
async def clear_credential(
    env_name: str, session: AsyncSession = Depends(get_session)
) -> Response:
    removed = await credentials.delete(session, env_name)
    if not removed:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            "credential_not_found",
            f"No stored credential for '{env_name}'.",
        )
    await session.commit()
    log.info("credential.cleared", env_name=env_name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
