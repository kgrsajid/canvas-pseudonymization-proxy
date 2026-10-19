"""Vault tests — SQLite full coverage + Postgres code path unit-tested via engine dialect."""

import pytest

from app.pseudonym import PseudonymError, compute_pseudonym
from app.vault import build_engine


async def test_lazy_population_and_reverse(vault_sqlite, settings_sqlite):
    p = await vault_sqlite.map_subject(3)
    assert p == compute_pseudonym(3, settings_sqlite.hmac_key)
    assert await vault_sqlite.reverse_lookup(p) == 3
    assert await vault_sqlite.entry_count() == 1


async def test_roundtrip_stability(vault_sqlite):
    p1 = await vault_sqlite.map_subject(3)
    p2 = await vault_sqlite.map_subject(3)  # second sight -> touch path
    assert p1 == p2
    assert await vault_sqlite.entry_count() == 1


async def test_unknown_reverse_returns_none(vault_sqlite):
    assert await vault_sqlite.reverse_lookup("P-AAAAAAAAAA") is None


async def test_malformed_reverse_returns_none(vault_sqlite):
    assert await vault_sqlite.reverse_lookup("garbage") is None
    assert await vault_sqlite.reverse_lookup("") is None


async def test_deterministic_across_restart(tmp_path, settings_sqlite):
    from app.vault import build_vault

    v1 = build_vault(settings_sqlite)
    await v1.init_schema()
    p = await v1.map_subject(10)
    await v1.close()
    # "restart": fresh vault instance, same key, same db file
    v2 = build_vault(settings_sqlite)
    await v2.init_schema()
    assert compute_pseudonym(10, settings_sqlite.hmac_key) == p
    assert await v2.reverse_lookup(p) == 10
    await v2.close()


async def test_key_recompute_regenerates_vault(tmp_path):
    """§6: forward mapping is pure HMAC — vault regenerable from roster."""
    from app.config import Settings
    from app.vault import build_vault

    key = "k" * 48
    s = Settings(hmac_key=key, vault_sqlite=str(tmp_path / "fresh.db"))
    v = build_vault(s)
    await v.init_schema()
    for uid in (3, 4, 5):
        p = await v.map_subject(uid)
        assert p == compute_pseudonym(uid, key)
    await v.close()


async def test_collision_fail_closed(vault_sqlite, monkeypatch):
    """Two different ids mapping to the same pseudonym must raise, never alias."""
    from app import vault as vault_mod

    orig = vault_mod.compute_pseudonym

    def evil(uid: int, key: str) -> str:
        return "P-COLLIDE0001" if uid in (11, 12) else orig(uid, key)

    monkeypatch.setattr(vault_mod, "compute_pseudonym", evil)
    await vault_sqlite.map_subject(11)
    with pytest.raises(PseudonymError):
        await vault_sqlite.map_subject(12)


async def test_audit_append_only(vault_sqlite):
    await vault_sqlite.audit("map", "P-AAAAAAAAAA", {"canvas_user_id": 3})
    await vault_sqlite.audit("resolve", None, {"context": "course:1"})
    tail = await vault_sqlite.audit_tail()
    events = [r["event"] for r in tail]
    assert events == ["resolve", "map"]  # newest first, both present
    assert all("ts" in r for r in tail)


async def test_no_names_columns():
    """Schema guarantee: vault tables contain no name/email columns."""
    from app.vault import audit_log, subject_map

    cols = set(subject_map.c.keys()) | set(audit_log.c.keys())
    forbidden = {"name", "email", "login_id", "sis_user_id", "sortable_name", "first_name"}
    assert not (cols & forbidden), f"vault schema leaks PII columns: {cols & forbidden}"


def test_postgres_engine_path(monkeypatch, tmp_path):
    """Postgres code path: DATABASE_URL -> postgresql+asyncpg engine (unit-level)."""
    from app.config import Settings

    for url in (
        "postgresql://u:p@localhost:5432/vault",
        "postgres://u:p@localhost:5432/vault",  # Dokku postgres:link form
        "postgresql+asyncpg://u:p@localhost:5432/vault",
    ):
        s = Settings(database_url=url, hmac_key="k" * 48)
        eng = build_engine(s)
        assert eng.dialect.name == "postgresql"
        assert eng.url.drivername == "postgresql+asyncpg"
        eng.sync_engine.dispose()


def test_sqlite_engine_path(tmp_path):
    from app.config import Settings

    s = Settings(vault_sqlite=str(tmp_path / "v.db"), hmac_key="k" * 48)
    eng = build_engine(s)
    assert eng.dialect.name == "sqlite"
    eng.sync_engine.dispose()


def test_no_backend_configured_fails(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VAULT_SQLITE", raising=False)
    s = Settings(hmac_key="k" * 48)
    import pytest as _pytest

    with _pytest.raises(RuntimeError):
        build_engine(s)
