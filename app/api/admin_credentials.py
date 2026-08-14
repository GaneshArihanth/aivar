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

    Reports the source because precedence is not obvious and decides what a
    "Set key" click will actually achieve: a value in the environment always
    wins, so overwriting the stored one would appear to do nothing.
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
        from_env = bool(os.environ.get(name) or getattr(settings, name.lower(), ""))
        out.append(
            {
                "env_name": name,
                "is_set": bool(providers.resolve_credential(name)),
                "source": "environment" if from_env else ("stored" if name in stored else None),
                "last4": stored.get(name) if not from_env else None,
                "editable": not from_env,
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
