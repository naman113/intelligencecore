"""Phase 1/review — connector helpers (pure): self-echo thread key + ERP dedupe."""

from app.connectors.db_poller import _row_fingerprint
from app.connectors.whatsapp_events import _thread_ref


def test_thread_ref_normalizes_direct_chat_phone():
    # Self-record side (normalized phone) and inbound side (raw jid) must converge.
    assert _thread_ref("+91 9000000101") == _thread_ref("919000000101@s.whatsapp.net")
    assert _thread_ref("919000000101") == "919000000101"


def test_thread_ref_prefers_group_jid():
    assert _thread_ref("919000000101", group_jid="grp-9@g.us") == "grp-9@g.us"


def test_row_fingerprint_distinguishes_rows_sharing_a_watermark():
    # Two rows with the same CreatedAt must not collide (would silently drop one).
    a = {"Id": 1, "CreatedAt": "2026-03-20T10:00:00", "Batch": "A"}
    b = {"Id": 2, "CreatedAt": "2026-03-20T10:00:00", "Batch": "B"}
    assert _row_fingerprint(a) != _row_fingerprint(b)
    # Stable for identical content.
    assert _row_fingerprint(a) == _row_fingerprint(dict(a))


# ---- Phase 14 (WH-03): propose_sources in warehouse mode ----
async def test_propose_sources_lists_base_tables_from_source_catalog(monkeypatch):
    from app import warehouse
    from app.connectors import db_poller
    seen = []

    async def fake(sql):
        seen.append(sql)
        return []

    async def no_sources():
        return []
    monkeypatch.setattr(db_poller, "run_readonly_sql", fake)
    monkeypatch.setattr(db_poller, "_load_sources", no_sources)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    await db_poller.propose_sources()
    assert "ic_meta.source_catalog" in seen[0] and "relkind IN ('r','p')" in seen[0]
    seen.clear()
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    await db_poller.propose_sources()
    assert "information_schema.tables" in seen[0] and "ic_meta" not in seen[0]
