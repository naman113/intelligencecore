"""22-19 Task 2: runner guards, argument parsing, chat handling and report merging (no network, no Docker)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dash_eval as de  # noqa: E402

ENV = {"SCENARIO_DEMO_STACK": "1", "OPS_KEY": "k" * 12}


def test_refuses_without_demo_flag():
    assert de.main(["run", "--stack", "p33", "--client", "mill", "--out", "x"], env={"OPS_KEY": "k"}) == 2


@pytest.mark.parametrize("stack", ["p3", "p32", "p6", "q33", "p100", "main", ""])
def test_refuses_bad_stack_names(stack):
    assert de.main(["setup", "--stack", stack, "--client", "mill"], env=dict(ENV)) == 2


@pytest.mark.parametrize("stack", ["p33", "p35", "p39", "p40", "p99"])
def test_accepts_test_stack_names(stack):
    de.check_guards(stack, ENV, *de.stack_urls(stack))


def test_default_urls_follow_the_stack():
    assert de.stack_urls("p33") == ("http://127.0.0.1:9331", "http://127.0.0.1:9332")


@pytest.mark.parametrize("url", ["http://example.com:9331", "http://10.0.0.5:9331",
                                 "http://my-app.railway.app", "https://x.up.railway.app"])
def test_refuses_non_loopback_and_railway(url):
    with pytest.raises(de.Refused):
        de.check_guards("p33", ENV, url, "http://127.0.0.1:9332")
    with pytest.raises(de.Refused):
        de.check_guards("p33", ENV, "http://127.0.0.1:9331", url)


def test_main_refuses_env_override_to_remote():
    env = dict(ENV, BASE_URL="http://203.0.113.9:9331")
    assert de.main(["setup", "--stack", "p33", "--client", "mill"], env=env) == 2


@pytest.mark.parametrize("name", ["agentic-app-1", "client-postgres-1", "client-app-1",
                                  "ic-p34-app-1", "ic-p3-app-1", "app-1"])
def test_refuses_other_containers(name):
    with pytest.raises(de.Refused):
        de.check_container("p33", name)


def test_accepts_own_containers():
    assert de.container("p33", "postgres-1") == "ic-p33-postgres-1"


def test_no_ops_key_refused():
    assert de.main(["run", "--stack", "p33", "--client", "mill", "--out", "x"],
                   env={"SCENARIO_DEMO_STACK": "1"}) == 2


def test_argument_parsing():
    a = de.build_parser().parse_args(["run", "--stack", "p34", "--client", "distributor", "--requests", "1,12",
                                      "--runs", "2", "--round", "1", "--budget", "5", "--out", "d"])
    assert (a.stack, a.client, a.requests, a.runs, a.round, a.budget, a.out) == (
        "p34", "distributor", "1,12", 2, 1, 5, "d")
    r = de.build_parser().parse_args(["report", "--out", "a", "--out", "b", "--write", "p"])
    assert r.out == ["a", "b"] and r.budget is None
    with pytest.raises(SystemExit):
        de.build_parser().parse_args(["run", "--stack", "p33", "--client", "nobody", "--out", "d"])


def test_today_is_the_operator_zone(monkeypatch):
    seen = {}

    class FakeDT:
        @staticmethod
        def now(tz):
            seen["tz"] = str(tz)
            import datetime
            return datetime.datetime(2026, 11, 5, 1, 0, tzinfo=tz)

    monkeypatch.setattr(de, "datetime", FakeDT)
    assert de.today_ist().isoformat() == "2026-11-05"
    assert seen["tz"] == "Asia/Kolkata"


# ------------------------------------------------------------------ run with a fake HTTP layer

class FakeApi:
    def __init__(self):
        self.asks, self.chats = [], 0

    def __call__(self, method, url, body=None, headers=None, timeout=60):
        path = url.split("?")[0]
        if path.endswith("/api/ask/chats"):
            self.chats += 1
            return 200, {"id": f"ui:eval:chat{self.chats}"}
        if path.endswith("/api/ask"):
            self.asks.append(body)
            return 200, {"run_id": 100 + len(self.asks)}
        if "/tools" in path:
            return 200, [{"tool": "use_skill", "args": '{"name": "x"}', "result_digest": "ok"}]
        return 200, []


@pytest.fixture
def fake(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(de, "http_json", api)
    monkeypatch.setattr(de, "wait_run", lambda ctx, rid, timeout=0: {"id": rid, "status": "done"})
    monkeypatch.setattr(de, "read_tokens", lambda ctx, rid: 1234)
    monkeypatch.setattr(de.Ctx, "psql", lambda self, sql, db="agent_memory", user="agent", stdin_sql=None: "0")
    monkeypatch.setattr(de, "sh", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    return api


def run_args(tmp_path, reqs):
    return de.build_parser().parse_args(["run", "--stack", "p33", "--client", "mill",
                                         "--requests", reqs, "--out", str(tmp_path / "o")])


def ctx():
    return de.Ctx("p33", "mill", "k" * 12, "http://127.0.0.1:9331", "http://127.0.0.1:9332")


def test_run_asks_request_12_in_request_1s_chat(fake, tmp_path):
    assert de.cmd_run(ctx(), run_args(tmp_path, "1,12")) == 0
    assert fake.chats == 1  # request 12 reuses the chat of request 1
    assert [a["chat"] for a in fake.asks] == ["ui:eval:chat1", "ui:eval:chat1"]
    assert all(a["operator"] == "eval" for a in fake.asks)
    recs = {json.loads(p.read_text())["n"]: json.loads(p.read_text()) for p in (tmp_path / "o").glob("*.json")}
    assert recs[1]["chat"] == recs[12]["chat"] == "ui:eval:chat1"
    assert recs[1]["tokens"] == 1234 and recs[1]["tools"][0]["name"] == "use_skill"
    assert "no draft" in recs[1]["errors"]


def test_run_requests_in_separate_chats(fake, tmp_path):
    assert de.cmd_run(ctx(), run_args(tmp_path, "1,2")) == 0
    assert fake.chats == 2
    assert fake.asks[0]["chat"] != fake.asks[1]["chat"]


def test_run_12_alone_records_predecessor_not_run(fake, tmp_path):
    assert de.cmd_run(ctx(), run_args(tmp_path, "12")) == 0
    assert fake.asks == [] and fake.chats == 0
    rec = json.loads(next((tmp_path / "o").glob("*.json")).read_text())
    assert rec["n"] == 12 and rec["errors"] == ["predecessor not run"]


def test_run_refuses_other_clients_request(fake, tmp_path):
    with pytest.raises(de.Refused):
        de.cmd_run(ctx(), run_args(tmp_path, "5"))  # request 5 is not mill's


def test_preview_digest_parsing():
    ok = de.parse_preview_digest('{"ok": true, "errors": [], "warnings": [{"rule": "tile_count"}]}')
    assert ok["ok"] and ok["warnings"][0]["rule"] == "tile_count"
    cut = de.parse_preview_digest('{"ok": true, "errors": [], "warnings": [{"rule": "a_rule", "mess')
    assert cut["ok"] and not cut["errors"] and cut["warnings"] == [{"rule": "a_rule"}]
    bad = de.parse_preview_digest('{"ok": false, "errors": [{"rule": "binding", "path": "tiles[0]')
    assert not bad["ok"] and bad["errors"]


# ------------------------------------------------------------------ report

def record(n, rnd, tokens=100, finished="2026-11-05T10:00:00Z"):
    return {"n": n, "round": rnd, "finished_at": finished, "spec": None, "render": None, "tools": [],
            "reply": "", "tokens": tokens, "model_calls": 5, "comparisons": [], "skips": [],
            "errors": ["no draft"]}


def test_report_keeps_latest_round(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "r1.json").write_text(json.dumps(record(1, 0, tokens=900)))
    (a / "r2.json").write_text(json.dumps(record(2, 0, tokens=300)))
    (b / "r1.json").write_text(json.dumps(record(1, 1, tokens=100, finished="2026-11-06T10:00:00Z")))
    rep = de.build_report(de.load_records([a, b]), None)
    row1 = next(r for r in rep["rows"] if r["n"] == 1)
    assert row1["round"] == 1 and row1["tokens"] == 100
    assert rep["total_tokens"] == 400 and rep["total_model_calls"] == 10
    assert rep["budget_suggestion"] == int(200 * 1.25)
    assert rep["gate_line"].startswith("eval gate: FAIL")
    out = tmp_path / "rep" / "x"
    args = de.build_parser().parse_args(["report", "--out", str(a), "--out", str(b), "--write", str(out)])
    assert de.cmd_report(args) == 0
    assert Path(str(out) + ".json").exists() and Path(str(out) + ".md").exists()


def test_report_markdown_keeps_token_shaped_strings_out(tmp_path):
    rec = record(1, 0)
    fo = tmp_path / "a"
    fo.mkdir()
    (fo / "r.json").write_text(json.dumps(rec))
    rep = de.build_report(de.load_records([fo]), None)
    leak = "sk-ant-api03-ABCDEFGHIJKLMNOP"
    ptok = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWo"
    rep["rows"][0]["notes"] += [f"key {leak}", f"url /d/x/v/1/?p={ptok}", f"also {ptok}",
                                "db postgresql://eval_ro:hunter2pw@postgres:5432/eval_x"]
    md = de.render_markdown(rep)
    for s in (leak, ptok, "hunter2pw"):
        assert s not in md
    args = de.build_parser().parse_args(["report", "--out", str(fo), "--write", str(tmp_path / "o")])
    de.cmd_report(args)
    assert "ABCDEFGHIJKLMNOP" not in Path(str(tmp_path / "o") + ".md").read_text()


@pytest.mark.parametrize("which", ["api", "dash"])
@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://localhost:8090", "http://127.0.0.1:9341",
                                 "http://127.0.0.1:99999", "http://127.0.0.1:abc"])
def test_guard_refuses_loopback_url_on_the_wrong_port(which, url):
    # Called directly: main() would go on into cmd_setup and docker if the guard regressed.
    api, dash = de.stack_urls("p33")
    args = (url, dash) if which == "api" else (api, url)
    with pytest.raises(de.Refused, match="port"):
        de.check_guards("p33", ENV, *args)


def test_secrets_file_wins_and_conflict_refused(tmp_path):
    f = tmp_path / "s.env"
    f.write_text("OPS_KEY=filekey123456\n")
    assert de.ops_key({}, str(f)) == "filekey123456"
    assert de.ops_key({"OPS_KEY": "filekey123456"}, str(f)) == "filekey123456"
    with pytest.raises(de.Refused):
        de.ops_key({"OPS_KEY": "other-key-xxxx"}, str(f))
    assert de.ops_key({"OPS_KEY": "envkey"}, "") == "envkey"
    with pytest.raises(de.Refused):  # an unreadable --secrets path is not silently ignored
        de.ops_key({"OPS_KEY": "envkey"}, str(tmp_path / "missing.env"))


def test_runs_above_one_refused():
    a = de.build_parser().parse_args(["run", "--stack", "p33", "--client", "mill", "--runs", "3",
                                      "--out", "x"])
    with pytest.raises(de.Refused):
        de.cmd_run(None, a)


def test_model_stages_order_ratio_after_simple():
    m = [{"object": "measure", "definition": {"name": "r", "type": "ratio"}},
         {"object": "measure", "definition": {"name": "s", "type": "simple"}},
         {"object": "relationship", "definition": {"name": "rel"}},
         {"object": "table", "definition": {"name": "t"}}]
    st = de.model_stages(m)
    assert [[de.item_name(x) for x in s] for s in st] == [["t"], ["rel"], ["s"], ["r"]]
    for client in de.CLIENTS:
        mm = json.loads((SCRIPTS / "dash_eval" / "models" / f"{client}.json").read_text(encoding="utf-8"))
        assert sum(len(s) for s in de.model_stages(mm)) == len(mm)


def test_refused_tool_result_fails():
    with pytest.raises(de.Fail):
        de.check_tool_result("ok a\nrefused: input measure x is not active; approve it first")
    de.check_tool_result("proposed 3 items")
    for text in ("REFUSED: items must be a non-empty list", "REFUSED: at most 12 items per call",
                 "TOOL ERROR x"):
        with pytest.raises(de.Fail):
            de.check_tool_result(text)


def test_psql_echo_of_alter_role_password_is_redacted():
    out = de.redact("LINE 2: ALTER ROLE eval_ro WITH LOGIN PASSWORD 'abcDEF123_-xyz'")
    assert "abcDEF123" not in out


def test_only_a_timeout_is_reported_as_timed_out(monkeypatch):
    class C:
        key = "k" * 12
        client = "mill"
        def call(self, *a, **k):
            return {"run_id": 1}
    monkeypatch.setattr(de, "wait_run", lambda *a, **k: (_ for _ in ()).throw(de.Fail("HTTP 401")))
    rec = de.run_request(C(), {"n": 1, "text": "x"}, "ui:eval:c", 0, {})
    assert "run timed out" not in rec["errors"] and any("HTTP 401" in e for e in rec["errors"])


def test_skip_active_items_on_rerun():
    items = [{"object": "table", "name": "a"}, {"object": "table", "name": "b"},
             {"object": "measure", "definition": {"name": "a"}}]
    left = de.skip_active(items, {"table": {"a"}, "measure": set()})
    assert [de.item_name(m) for m in left] == ["b", "a"]
    assert left[1]["object"] == "measure"


def test_run_request_survives_unexpected_errors(monkeypatch):
    class C:
        key = "k" * 12
        client = "mill"
        def call(self, *a, **k):
            return {"run_id": 1}
    monkeypatch.setattr(de, "wait_run", lambda *a, **k: (_ for _ in ()).throw(de.RunTimeout("slow")))
    monkeypatch.setattr(de, "convert_tools", lambda rows: (_ for _ in ()).throw(ValueError("boom")))
    rec = de.run_request(C(), {"n": 1, "text": "x"}, "ui:eval:c", 0, {})
    assert "run timed out" in rec["errors"] and any("ValueError" in e for e in rec["errors"])
    assert rec["finished_at"]


def test_propose_in_app_applies_config_overrides(monkeypatch):
    """22-16: the exec'd process starts with the container env (demo business DB); it must load the
    stored config overrides first, or it models the wrong database."""
    seen = {}

    def fake_sh(cmd, prog=None, **k):
        seen["prog"] = prog
        return subprocess.CompletedProcess(cmd, 0, 'RESULT "ok"\n', "")

    monkeypatch.setattr(de, "sh", fake_sh)
    monkeypatch.setattr(de, "check_container", lambda *a, **k: None)
    ctx = type("C", (), {"stack": "p33", "app": "ic-p33-app-1"})()
    de.propose_in_app(ctx, [{"object": "table"}])
    prog = seen["prog"]
    assert "configservice.load_overrides()" in prog
    assert prog.index("load_overrides") < prog.index("propose_model_change")


def test_run_sends_new_dashboard_requests_as_the_ui_does(fake, tmp_path):
    """22-16: the owner's UI opens the chat with 'Build a dashboard: <what>' (frontend asks.dashboard);
    a bare topic such as 'Who owes me money' is answered in chat. An edit (request 12) goes bare."""
    assert de.cmd_run(ctx(), run_args(tmp_path, "1,2,12")) == 0
    msgs = [a["message"] for a in fake.asks]
    assert msgs[1] == "Build a dashboard: Who owes me money and how old is it?"
    assert msgs[0].startswith("Build a dashboard: ")
    assert msgs[2] == "Make the outward bars red"
