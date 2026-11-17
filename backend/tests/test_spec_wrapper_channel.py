"""Phase 22 plan 05 (DCAP-14, C-12): the spec-only wrapper and its strict message channel."""
import json
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import dashboards, main
from app import dashboards_serve as ds

P = {"X-IC-Dashboards-Proxy": "1"}
TOKEN = "view-token-123"
V2 = "ic-render-2.0.0"
V1 = "ic-render-1.0.0"


class Conn:
    """vid -> (kind, renderer); versions: key (None = current, int = pinned) -> (status, vid, number)."""

    def __init__(self):
        self.versions = {None: ("published", 11, 7), 7: ("published", 11, 7), 2: ("draft", 12, 2),
                         3: ("published", 13, 3), 4: ("published", 14, 4), 5: ("published", 15, 5)}
        self.kinds = {11: ("spec", V2), 12: ("spec", V2), 13: ("spec", V1), 14: ("html", None),
                      15: ("spec", V2)}
        self.calls = []
        self.version_none = False

    async def fetchrow(self, sql, *a):
        self.calls.append(sql)
        if sql in (ds.CURRENT_AUTH_SQL, ds.PINNED_AUTH_SQL):
            key = None if sql == ds.CURRENT_AUTH_SQL else a[1]
            if a[0] != "sales" or key not in self.versions:
                return None
            status, vid, _ = self.versions[key]
            return {"view_token": TOKEN, "version_id": vid, "status": status}
        vid = a[0]
        kind, renderer = self.kinds.get(vid, (None, None))
        if sql == ds.FORMAT_SQL:
            return None if kind is None else {"is_spec": kind == "spec", "renderer": renderer}
        if sql == ds.VERSION_SQL:
            if self.version_none:
                return None
            return {"version": next(n for (_, v, n) in self.versions.values() if v == vid)}
        return None

    async def fetchval(self, sql, *a):
        return None


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self, timeout=None):
        c = self.conn

        class A:
            async def __aenter__(self_):
                return c

            async def __aexit__(self_, *e):
                return False
        return A()


@pytest.fixture
def env(monkeypatch):
    conn = Conn()

    async def get_pool():
        return Pool(conn)
    monkeypatch.setattr(ds, "db", SimpleNamespace(get_pool=get_pool))

    async def ok(c, *, version_id, token):
        return token == "GOOD" and version_id == 12
    monkeypatch.setattr(dashboards, "preview_token_ok", ok)
    monkeypatch.setattr(ds.dashsettings, "ask_base", lambda: "https://app.example.com")
    return SimpleNamespace(conn=conn, client=TestClient(main.app, follow_redirects=False))


def _get(env, path):
    return env.client.get(path, headers=P)


def _attr(html, name):
    return re.search(name + r'="([^"]*)"', html).group(1)


# ---- constants and the builder ------------------------------------------------

def test_spec_wrapper_csp_differs_only_in_the_script_hash():
    assert ds.SPEC_WRAPPER_SCRIPT_HASH != ds.WRAPPER_SCRIPT_HASH
    assert ds.SPEC_WRAPPER_CSP == ds.WRAPPER_CSP.replace(ds.WRAPPER_SCRIPT_HASH, ds.SPEC_WRAPPER_SCRIPT_HASH)
    assert ds.WRAPPER_SCRIPT_HASH not in ds.SPEC_WRAPPER_CSP


def test_spec_wrapper_html_shape():
    h = ds.spec_wrapper_html("https://app.example.com:8443", "./v/7/frame")
    assert '<body data-ask-base="https://app.example.com:8443" data-frame="./v/7/frame">' in h
    assert '<a id="ic-ask" hidden target="_blank" rel="noopener noreferrer"></a>' in h
    assert '<iframe sandbox="allow-scripts" referrerpolicy="no-referrer" title="Dashboard"></iframe>' in h
    assert "<script>" + ds.SPEC_WRAPPER_SCRIPT + "</script>" in h
    assert "<iframe sandbox=\"allow-scripts\" referrerpolicy=\"no-referrer\" title=\"Dashboard\" src" not in h
    assert TOKEN not in h
    assert ds.spec_wrapper_html("https://app.example.com:8443", "./v/7/frame") == h


@pytest.mark.parametrize("base", ["", "ftp://x.example.com", "https://x.example.com/", "https://x.example.com/p",
                                  'https://x"><script>', "https://x.example.com:99999x", "//x.example.com"])
def test_bad_ask_base_becomes_empty(base):
    assert 'data-ask-base=""' in ds.spec_wrapper_html(base, "./frame")


@pytest.mark.parametrize("path", ["frame", "./v/0/frame", "./v/07/frame", "./v/7/../frame", "//evil/frame",
                                  "javascript:x", './frame"', "./v/1234567890/frame", ""])
def test_bad_frame_path_raises(path):
    with pytest.raises(ValueError):
        ds.spec_wrapper_html("", path)


def test_script_never_uses_sinks_or_posts():
    s = ds.SPEC_WRAPPER_SCRIPT
    for bad in ("innerHTML", "outerHTML", "eval", "Function", "window.open", "postMessage", "location =",
                "location.href", "location.assign", "location.replace", "document.write"):
        assert bad not in s, bad
    assert s.isascii()
    assert "</" not in s


# ---- serve branch -------------------------------------------------------------

def test_unpinned_v2_wrapper_names_its_version_frame(env):
    r = _get(env, f"/d/sales/?t={TOKEN}")
    assert r.status_code == 200
    assert _attr(r.text, "data-frame") == "./v/7/frame"
    assert _attr(r.text, "data-ask-base") == "https://app.example.com"
    assert r.headers["content-security-policy"] == ds.SPEC_WRAPPER_CSP
    assert r.headers["cache-control"] == "no-store"
    assert ds.VERSION_SQL in env.conn.calls


def test_pinned_and_draft_v2_wrappers_use_plain_frame(env):
    for path in (f"/d/sales/v/7/?t={TOKEN}", "/d/sales/v/2/?p=GOOD"):
        env.conn.calls.clear()
        r = _get(env, path)
        assert r.status_code == 200, path
        assert _attr(r.text, "data-frame") == "./frame"
        assert ds.VERSION_SQL not in env.conn.calls


def test_v1_spec_and_legacy_html_keep_todays_wrapper(env):
    for path in (f"/d/sales/v/3/?t={TOKEN}", f"/d/sales/v/4/?t={TOKEN}"):
        env.conn.calls.clear()
        r = _get(env, path)
        assert r.status_code == 200, path
        assert r.text == ds.WRAPPER_HTML
        assert r.headers["content-security-policy"] == ds.WRAPPER_CSP
        assert ds.VERSION_SQL not in env.conn.calls


def test_missing_version_row_is_404_never_unpinned_frame(env):
    env.conn.version_none = True
    r = _get(env, f"/d/sales/?t={TOKEN}")
    assert r.status_code == 404
    assert "data-frame" not in r.text


def test_wrong_token_is_404(env):
    assert _get(env, "/d/sales/?t=nope").status_code == 404


# ---- the script, driven in node:vm --------------------------------------------

HARNESS = r"""
import vm from 'node:vm';
import fs from 'node:fs';
const script = fs.readFileSync(process.argv[2], 'utf8');

function mk(opts) {
  const frameWin = { name: 'frame' };
  const iframe = { contentWindow: frameWin, src: undefined };
  const link = { textContent: '', href: '', hidden: true };
  const replaced = [];
  let handler = null;
  const attrs = { 'data-ask-base': opts.base, 'data-frame': opts.frame };
  const ctx = {
    location: { search: opts.search || '', hash: opts.hash || '', pathname: opts.path || '/d/sales/' },
    history: { replaceState: (...a) => replaced.push(a) },
    document: {
      querySelector: (s) => (s === 'iframe' ? iframe : null),
      getElementById: (id) => (id === 'ic-ask' ? link : null),
      body: { getAttribute: (n) => (n in attrs ? attrs[n] : null) },
    },
    addEventListener: (t, f) => { if (t === 'message') handler = f; },
    encodeURIComponent, Array, Object,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(script, ctx);
  return { iframe, link, replaced, frameWin,
    send: (data, source) => handler({ data, source: source === undefined ? frameWin : source }) };
}

let failed = [];
function check(name, cond) { if (!cond) failed.push(name); }
const BASE = 'https://app.example.com';
const st = (h) => ({ ic: 1, type: 'state', hash: h });
const ask = (title, tile) => ({ ic: 1, type: 'ask', tile: tile || 't1', title });

{
  const w = mk({ base: BASE, frame: './v/7/frame', search: '?t=x', hash: '#r=last_7_days' });
  check('src', w.iframe.src === './v/7/frame?t=x#r=last_7_days');
  w.send(st('r=last_7_days;f.region=North%2CSouth'));
  check('state ok', w.replaced.length === 1 && w.replaced[0][0] === null && w.replaced[0][1] === ''
    && w.replaced[0][2] === '#r=last_7_days;f.region=North%2CSouth');
  w.send(st('r=last_7_days'), { name: 'other' });
  check('state other source', w.replaced.length === 1);
  w.send({ ic: 1, type: 'state', hash: 'a', extra: 1 });
  check('state extra key', w.replaced.length === 1);
  w.send(st('a b'));
  w.send(st('a<b'));
  w.send(st('a'.repeat(601)));
  check('state bad hash', w.replaced.length === 1);
  w.send(st('a'.repeat(600)));
  check('state 600 ok', w.replaced.length === 2);
  w.send({ ic: 2, type: 'state', hash: 'a' });
  w.send({ ic: 1, type: 'nope' });
  w.send('state');
  w.send(null);
  w.send([1]);
  check('state shapes', w.replaced.length === 2);

  w.send(ask('Revenue by day'));
  check('ask text', w.link.textContent === 'Ask about: Revenue by day' && w.link.hidden === false);
  check('ask href', w.link.href === BASE + '/?ask=' + encodeURIComponent('Explain "Revenue by day" on the dashboard sales'));
}
for (const [name, title] of [['control', 'a\u0007b'], ['newline', 'a\nb'], ['bidi 202E', 'a‮b'],
    ['bidi 2066', 'a⁦b'], ['lone high', 'a\uD800b'], ['lone low', 'a\uDC00'], ['empty', ''],
    ['121 chars', 'x'.repeat(121)], ['number', 5]]) {
  const w = mk({ base: BASE, frame: './frame' });
  w.send(ask(title));
  check('ask refuses ' + name, w.link.hidden === true && w.link.textContent === '');
}
{
  const w = mk({ base: BASE, frame: './frame' });
  w.send(ask('x'.repeat(120)));
  check('120 ok', w.link.hidden === false);
  const e = mk({ base: BASE, frame: './frame' });
  e.send(ask('😀'.repeat(120)));
  check('120 code points of surrogate pairs ok', e.link.hidden === false);
  const f = mk({ base: BASE, frame: './frame' });
  f.send(ask('😀'.repeat(121)));
  check('121 code points refused', f.link.hidden === true);
  const g = mk({ base: BASE, frame: './frame' });
  g.send(ask('ok', 'Bad Tile'));
  g.send({ ic: 1, type: 'ask', tile: 't1', title: 'ok', extra: 1 });
  g.send({ ic: 1, type: 'ask', tile: 't1' });
  g.send(ask('ok'), { name: 'other' });
  check('ask bad tile, extra key, missing key, other source', g.link.hidden === true);
}
{
  const w = mk({ base: '', frame: './frame' });
  w.send(ask('Revenue'));
  check('empty base: no link', w.link.hidden === true);
  const x = mk({ base: 'javascript:alert(1)', frame: './frame' });
  x.send(ask('Revenue'));
  check('bad base: no link', x.link.hidden === true);
  const y = mk({ base: BASE, frame: './frame', path: '/elsewhere/' });
  y.send(ask('Revenue'));
  check('no slug: no link', y.link.hidden === true);
  const z = mk({ base: BASE, frame: './frame', path: '/d/sales/v/3/' });
  z.send(ask('Revenue'));
  check('pinned path slug', z.link.href.endsWith(encodeURIComponent('Explain "Revenue" on the dashboard sales')));
}
for (const bad of ['//evil.example/frame', './v/7/../frame', 'javascript:x', 'frame', './v/0/frame', '', null]) {
  const w = mk({ base: BASE, frame: bad, search: '?t=x' });
  check('frame path refused ' + bad, w.iframe.src === undefined);
}
{
  const w = mk({ base: BASE, frame: './frame', search: '?t=x' });
  check('plain frame src', w.iframe.src === './frame?t=x');
}
if (failed.length) { console.log('FAILED: ' + failed.join(' | ')); process.exit(1); }
console.log('OK');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed; the channel script test needs it")
def test_wrapper_script_channel_in_node_vm(tmp_path):
    (tmp_path / "script.js").write_text(ds.SPEC_WRAPPER_SCRIPT, encoding="utf-8")
    (tmp_path / "harness.mjs").write_text(HARNESS, encoding="utf-8")
    p = subprocess.run(["node", str(tmp_path / "harness.mjs"), str(tmp_path / "script.js")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and p.stdout.strip() == "OK", p.stdout + p.stderr


def test_script_is_valid_json_free_of_template_markers():
    assert json.dumps(ds.SPEC_WRAPPER_SCRIPT)
    assert "{{" not in ds.SPEC_WRAPPER_SCRIPT
