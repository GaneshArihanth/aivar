"""Provider credentials submitted through the dashboard.

A deployed instance has no shell for most operators: keys live in SSM, and
changing one meant editing a parameter and redeploying. This lets a key be set
from the web page instead, which is the only practical route on AWS.

Two constraints shape the design.

**The value is never readable back.** No endpoint returns it, no log line
contains it, and the dashboard is told only the last four characters and when
it changed. A dashboard that can display a key is a dashboard that leaks one,
and this one has no authentication in front of it.

**Lookups are synchronous.** ``providers.resolve_credential`` is called from
``resolve_route``, which is sync and sits on the dispatch path. Reaching into
an async database there would mean either blocking the event loop or making the
whole call chain async for a value that changes perhaps twice a year. So
credentials are mirrored in memory at startup and refreshed on write — the same
approach ``pricing`` already takes, and for the same reason.
"""

from __future__ import annotations

import base64
import hashlib

import structlog
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import ProviderCredential

log = structlog.get_logger(__name__)


def _fernet() -> Fernet:
    """Derive the encryption key from the pepper already required in production.

    Reusing API_KEY_PEPPER avoids a second secret that must be set, kept, and
    never rotated casually — and it inherits the existing operational rule that
    losing the pepper invalidates stored secrets. That coupling is deliberate
    but worth stating: change the pepper and these credentials become
    undecryptable, exactly as agent keys become unverifiable.
    """
    digest = hashlib.sha256(settings.api_key_pepper.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


# env var name -> plaintext value. Populated at startup, refreshed on write.
_cache: dict[str, str] = {}


async def load(session: AsyncSession) -> None:
    """Mirror stored credentials into memory."""
    rows = list((await session.execute(select(ProviderCredential))).scalars())
    fernet = _fernet()

    resolved: dict[str, str] = {}
    for row in rows:
        try:
            resolved[row.env_name] = fernet.decrypt(row.ciphertext).decode("utf-8")
        except InvalidToken:
            # Almost always a changed API_KEY_PEPPER. Skip the row rather than
            # failing startup: the rest of the system is fine, and the operator
            # needs a running dashboard to re-enter the key.
            log.warning("credential.undecryptable", env_name=row.env_name)

    _cache.clear()
    _cache.update(resolved)
    log.info("credentials.loaded", count=len(_cache))


def get(env_name: str) -> str | None:
    return _cache.get(env_name) or None


def describe() -> dict[str, str]:
    """Last four characters per stored key, for display. Never the value."""
    return {name: value[-4:] for name, value in _cache.items()}


async def put(session: AsyncSession, env_name: str, value: str) -> ProviderCredential:
    """Encrypt and store one credential, replacing any previous value."""
    ciphertext = _fernet().encrypt(value.encode("utf-8"))

    row = (
        await session.execute(
            select(ProviderCredential).where(ProviderCredential.env_name == env_name)
        )
    ).scalar_one_or_none()

    if row is None:
        row = ProviderCredential(env_name=env_name)
        session.add(row)

    row.ciphertext = ciphertext
    # Stored separately so the dashboard can show which key is configured
    # without the server ever having to decrypt one to render a page.
    row.last4 = value[-4:]

    _cache[env_name] = value
    return row


async def delete(session: AsyncSession, env_name: str) -> bool:
    row = (
        await session.execute(
            select(ProviderCredential).where(ProviderCredential.env_name == env_name)
        )
    ).scalar_one_or_none()
    if row is None:
        return False

    await session.delete(row)
    _cache.pop(env_name, None)
    return True
