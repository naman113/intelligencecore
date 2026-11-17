"""whatsapp.fetch_group_members — the gateway side of the roster contract.

Fail-soft is the whole point: the caller (chats.sync_members) writes NOTHING
on None, so every failure mode here must collapse to None rather than raise or
return a half-parsed payload. Fake httpx client; no network.
"""

from types import SimpleNamespace

from app import whatsapp


class FakeResponse:
    def __init__(self, status=200, body=None, raises=None):
        self.is_success = 200 <= status < 300
        self.status_code = status
        self._body = body if body is not None else {}
        self._raises = raises

    def json(self):
        if self._raises:
            raise self._raises
        return self._body


class FakeClient:
    """Async-context-manager stand-in for httpx.AsyncClient."""

    last = None   # the most recent instance, for call assertions

    def __init__(self, *, base_url="", timeout=None):
        self.base_url = base_url
        self.timeout = timeout
        self.gets = []
        FakeClient.last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path, params=None, headers=None):
        self.gets.append({"path": path, "params": params, "headers": headers})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _wire(monkeypatch, response, *, base="http://connector:3000", key="k"):
    monkeypatch.setattr(
        whatsapp, "get_settings",
        lambda: SimpleNamespace(wa_api_base=base, wa_api_key=key))

    def factory(*, base_url="", timeout=None):
        c = FakeClient(base_url=base_url, timeout=timeout)
        c.response = response
        return c

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_happy_path_returns_subject_and_members(monkeypatch):
    _wire(monkeypatch, FakeResponse(body={
        "success": True, "subject": "Site B Ops",
        "members": [{"number": "919000000102", "admin": "admin"}]}))
    out = await whatsapp.fetch_group_members("g1@g.us")
    assert out == {"subject": "Site B Ops",
                   "members": [{"number": "919000000102", "admin": "admin"}]}
    call = FakeClient.last.gets[0]
    assert call["path"] == "/api/group-members"
    assert call["params"] == {"jid": "g1@g.us"}
    assert call["headers"] == {"x-api-key": "k"}


async def test_unconfigured_returns_none(monkeypatch):
    _wire(monkeypatch, FakeResponse(), base="")
    assert await whatsapp.fetch_group_members("g1@g.us") is None


async def test_non_2xx_returns_none(monkeypatch):
    _wire(monkeypatch, FakeResponse(status=503))
    assert await whatsapp.fetch_group_members("g1@g.us") is None


async def test_connection_error_returns_none(monkeypatch):
    _wire(monkeypatch, RuntimeError("connection refused"))
    assert await whatsapp.fetch_group_members("g1@g.us") is None


async def test_success_false_returns_none(monkeypatch):
    _wire(monkeypatch, FakeResponse(body={"success": False, "members": []}))
    assert await whatsapp.fetch_group_members("g1@g.us") is None


async def test_malformed_members_returns_none(monkeypatch):
    _wire(monkeypatch, FakeResponse(body={"success": True, "members": "oops"}))
    assert await whatsapp.fetch_group_members("g1@g.us") is None


async def test_bad_json_returns_none(monkeypatch):
    _wire(monkeypatch, FakeResponse(raises=ValueError("not json")))
    assert await whatsapp.fetch_group_members("g1@g.us") is None


# ── fetch_qr (the dashboard pairing page's data source) ────────────────────

async def test_fetch_qr_happy_path(monkeypatch):
    _wire(monkeypatch, FakeResponse(body={
        "success": True, "connected": False, "qr": "data:image/png;base64,abc"}))
    out = await whatsapp.fetch_qr()
    assert out == {"connected": False, "qr": "data:image/png;base64,abc",
                   "user": None}
    assert FakeClient.last.gets[0]["path"] == "/api/qr-data"
    assert FakeClient.last.gets[0]["headers"] == {"x-api-key": "k"}


async def test_fetch_qr_fails_soft(monkeypatch):
    _wire(monkeypatch, RuntimeError("connection refused"))
    assert await whatsapp.fetch_qr() is None
    _wire(monkeypatch, FakeResponse(status=503))
    assert await whatsapp.fetch_qr() is None
    _wire(monkeypatch, FakeResponse(), base="")
    assert await whatsapp.fetch_qr() is None
