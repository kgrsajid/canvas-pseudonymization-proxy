"""Dual-backend vault: PostgreSQL (production default) / SQLite (dev).

Schema per design §6: subject_map + append-only audit_log. NO names ever stored —
the vault is a pure pseudonym <-> canvas_user_id map plus audit trail.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import Column, Integer, MetaData, Table, Text, insert, select, update, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .config import Settings
from .pseudonym import compute_pseudonym, is_valid_pseudonym, PseudonymError

metadata = MetaData()

subject_map = Table(
    "subject_map",
    metadata,
    Column("pseudonym", Text, primary_key=True),
    Column("canvas_user_id", Integer, nullable=False, unique=True),
    Column("created_at", Text, nullable=False),
    Column("last_seen_at", Text),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Text, nullable=False),
    Column("event", Text, nullable=False),  # map|resolve|send|dead_letter|rotate
    Column("pseudonym", Text),
    Column("detail_json", Text),
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Vault:
    """Async vault over either backend. All writes go through here."""

    def __init__(self, engine: AsyncEngine, settings: Settings):
        self.engine = engine
        self.settings = settings
        self._kind = "postgresql" if engine.dialect.name == "postgresql" else "sqlite"

    async def init_schema(self) -> None:
        from sqlalchemy.schema import CreateTable

        async with self.engine.begin() as conn:
            for table in (subject_map, audit_log):
                await conn.execute(CreateTable(table, if_not_exists=True))

    # ---- forward (id -> pseudonym, lazy population + collision check) ----

    async def map_subject(self, canvas_user_id: int) -> str:
        """Return the pseudonym for a real id, creating the mapping on first sight.

        Collision check: if HMAC output collides with a different id (prob ~5e-8
        for 10k students), we FAIL CLOSED with an error rather than alias two
        students onto one pseudonym.
        """
        pseudonym = compute_pseudonym(canvas_user_id, self.settings.hmac_key)
        existing = await self.reverse_lookup(pseudonym)
        if existing is not None:
            if existing != canvas_user_id:
                raise PseudonymError(
                    f"pseudonym collision: {pseudonym} already mapped to id {existing}, "
                    f"refusing to alias id {canvas_user_id} (fail-closed)"
                )
            await self._touch(pseudonym)
            return pseudonym
        now = _utcnow()
        try:
            async with self.engine.begin() as conn:
                await conn.execute(
                    insert(subject_map).values(
                        pseudonym=pseudonym, canvas_user_id=canvas_user_id, created_at=now, last_seen_at=now
                    )
                )
        except IntegrityError:
            # UNIQUE(canvas_user_id) race: another id? re-check and fail closed on mismatch.
            existing = await self.reverse_lookup(pseudonym)
            if existing != canvas_user_id:
                raise PseudonymError("pseudonym collision detected at write (fail-closed)")
        await self.audit("map", pseudonym, {"canvas_user_id": canvas_user_id})
        return pseudonym

    async def _touch(self, pseudonym: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(subject_map).where(subject_map.c.pseudonym == pseudonym).values(last_seen_at=_utcnow())
            )

    # ---- reverse (pseudonym -> id). Unknown -> None, caller decides. ----

    async def reverse_lookup(self, pseudonym: str) -> int | None:
        if not is_valid_pseudonym(pseudonym):
            return None
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(subject_map.c.canvas_user_id).where(subject_map.c.pseudonym == pseudonym)
                )
            ).first()
        return row[0] if row else None

    async def entry_count(self) -> int:
        async with self.engine.connect() as conn:
            return (await conn.execute(select(func.count()).select_from(subject_map))).scalar_one()

    # ---- audit (append-only) ----

    async def audit(self, event: str, pseudonym: str | None, detail: dict[str, Any] | None = None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                insert(audit_log).values(
                    ts=_utcnow(),
                    event=event,
                    pseudonym=pseudonym,
                    detail_json=json.dumps(detail, ensure_ascii=False) if detail else None,
                )
            )

    async def audit_tail(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(select(audit_log).order_by(audit_log.c.id.desc()).limit(limit))
            ).fetchall()
        cols = audit_log.columns.keys()
        return [dict(zip(cols, r)) for r in rows]

    async def close(self) -> None:
        await self.engine.dispose()


class VaultFactory(Protocol):
    async def __call__(self) -> Vault: ...


def build_engine(settings: Settings) -> AsyncEngine:
    if settings.database_url:
        url = settings.database_url
        # Dokku postgres:link emits postgres://; SQLAlchemy needs postgresql+asyncpg://
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                url = "postgresql+asyncpg://" + url[len(prefix):]
                break
        else:
            if not url.startswith("postgresql+asyncpg://"):
                url = url  # custom dialect URL (e.g. sqlite+aiosqlite) — pass through
        return create_async_engine(url, pool_pre_ping=True)
    if settings.vault_sqlite:
        return create_async_engine(f"sqlite+aiosqlite:///{settings.vault_sqlite}")
    raise RuntimeError("vault backend not configured: set DATABASE_URL (prod) or VAULT_SQLITE (dev)")


def build_vault(settings: Settings) -> Vault:
    return Vault(build_engine(settings), settings)
