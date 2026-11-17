"""Group membership roster (chats.sync_members and friends) — pure logic.

The roster is ground truth for "who is in this group"; without it the reactive
agent invents membership answers. These tests pin the replace-style sync, the
never-wipe-on-failure rule, and the webhook staleness/throttle gate.
No DB / connector / network — fakes only.
"""

import inspect
from datetime import datetime, timedelta, timezone

from app import chats


class FakeConn:
    def __init__(self):
        self.executes = []

    async def execute(self, sql, *args):
        self.executes.append((sql, args))

    def transaction(self):
        class Tx:
            async def __aenter__(self_tx):
                return None

            async def __aexit__(self_tx, *exc):
                return False
        return Tx()


class FakePool:
    def __init__(self):
        self.conn = FakeConn()

    def acquire(self):
        class Ctx:
            def __init__(self_c, conn):
                self_c.conn = conn

            async def __aenter__(self_c):
                return self_c.conn

            async def __aexit__(self_c, *exc):
                return False
        return Ctx(self.conn)


def _wire(monkeypatch, roster):
    """Fake the connector fetch + pool; returns the FakeConn for assertions."""
    pool = FakePool()

    async def fake_get_pool():
        return pool

    async def fake_fetch(jid):
        return roster

    monkeypatch.setattr(chats, "get_pool", fake_get_pool)
    import app.whatsapp
    monkeypatch.setattr(app.whatsapp, "fetch_group_members", fake_fetch)
    return pool.conn


# ── sync_members ───────────────────────────────────────────────────────────

async def test_sync_upserts_dedupes_and_deletes_absent(monkeypatch):
    conn = _wire(monkeypatch, {
        "subject": "Site B Ops",
        "members": [
            {"number": "919000000102", "admin": "superadmin"},
            {"number": "917000000001", "admin": None},
            {"number": "917000000001", "admin": "admin"},  # dup keeps admin
            {"number": "", "admin": None},                 # junk dropped
        ]})

    n = await chats.sync_members("g1@g.us")
    assert n == 2

    inserts = [(s, a) for s, a in conn.executes if "INSERT INTO chat_members" in s]
    assert len(inserts) == 2
    by_num = {a[1]: a[2] for _, a in inserts}
    assert by_num == {"919000000102": True, "917000000001": True}

    deletes = [(s, a) for s, a in conn.executes if "DELETE FROM chat_members" in s]
    assert len(deletes) == 1
    sql, args = deletes[0]
    assert "NOT (number = ANY($2::text[]))" in sql   # replace-style: absent go
    assert sorted(args[1]) == ["917000000001", "919000000102"]

    subj = [(s, a) for s, a in conn.executes if "members_synced_at = now()" in s]
    assert len(subj) == 1
    assert "COALESCE($2, subject)" in subj[0][0]     # None never blanks it
    assert subj[0][1][1] == "Site B Ops"


async def test_connector_failure_writes_nothing(monkeypatch):
    # A flaky connector must never wipe a previously-good roster.
    conn = _wire(monkeypatch, None)
    assert await chats.sync_members("g1@g.us") is None
    assert conn.executes == []


async def test_sync_normalizes_numbers(monkeypatch):
    conn = _wire(monkeypatch, {
        "subject": None,
        "members": [{"number": "0919000000102", "admin": None}]})
    await chats.sync_members("g1@g.us")
    inserts = [a for s, a in conn.executes if "INSERT INTO chat_members" in s]
    assert inserts[0][1] == "919000000102"     # trunk zeros stripped


# ── maybe_sync_members (webhook staleness gate) ────────────────────────────

def _gate(monkeypatch):
    """Capture scheduling instead of creating real tasks."""
    scheduled = []
    monkeypatch.setattr(chats, "_schedule_sync",
                        lambda jid: (scheduled.append(jid), True)[1])
    chats._last_sync_attempt.clear()
    return scheduled


def _row(**over):
    row = {"jid": "g1@g.us", "is_group": True, "is_new": False,
           "members_synced_at": datetime.now(timezone.utc)}
    row.update(over)
    return row


def test_new_group_schedules(monkeypatch):
    s = _gate(monkeypatch)
    assert chats.maybe_sync_members(_row(is_new=True)) is True
    assert s == ["g1@g.us"]


def test_never_synced_schedules(monkeypatch):
    s = _gate(monkeypatch)
    assert chats.maybe_sync_members(_row(members_synced_at=None)) is True
    assert s == ["g1@g.us"]


def test_stale_roster_schedules(monkeypatch):
    s = _gate(monkeypatch)
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    assert chats.maybe_sync_members(_row(members_synced_at=old)) is True
    assert s == ["g1@g.us"]


def test_fresh_roster_does_not_schedule(monkeypatch):
    s = _gate(monkeypatch)
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    assert chats.maybe_sync_members(_row(members_synced_at=fresh)) is False
    assert s == []


def test_throttle_blocks_repeat_attempts(monkeypatch):
    # A downed connector must not be retried on every single group message.
    s = _gate(monkeypatch)
    assert chats.maybe_sync_members(_row(members_synced_at=None)) is True
    assert chats.maybe_sync_members(_row(members_synced_at=None)) is False
    assert s == ["g1@g.us"]


def test_dm_row_never_schedules(monkeypatch):
    s = _gate(monkeypatch)
    assert chats.maybe_sync_members(_row(is_group=False, is_new=True)) is False
    assert s == []


def test_garbage_row_fails_soft(monkeypatch):
    _gate(monkeypatch)
    assert chats.maybe_sync_members({}) is False       # must not raise


# ── read-path contracts (source pins, matching test_chats.py style) ────────

def test_list_members_resolves_primary_and_alt_numbers():
    src = inspect.getsource(chats.list_members)
    assert "ANY(p.whatsapp_alt)" in src     # alt numbers name the member too
    assert "p.active" in src                # inactive people don't resolve
    assert "LEFT JOIN" in src               # unknown members still listed


def test_list_chats_carries_member_counts():
    src = inspect.getsource(chats.list_chats)
    assert "member_count" in src and "known_count" in src
    assert "members_synced_at" in src


def test_set_status_activation_schedules_a_sync():
    # Activation is the moment the roster starts to matter; the hook lives in
    # set_status so the WhatsApp-DM activation path gets it too, not just the
    # dashboard PATCH.
    src = inspect.getsource(chats.set_status)
    assert 'if status == "active"' in src and "_schedule_sync" in src
