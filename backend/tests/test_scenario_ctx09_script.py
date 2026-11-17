"""Quick 015: pure-function tests for scripts/scenario_ctx09.py (the 7c live gate)."""

import importlib.util
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scenario_ctx09.py"


def _load():
    spec = importlib.util.spec_from_file_location("scenario_ctx09_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sc = _load()


@pytest.mark.parametrize("reply,created,history,want", [
    # the p18 live case: the new watch is quoted, the step-3 watch on the same thread is recalled
    ("I've set up watch #2. It checks every hour. Your earlier watch #1 uses a similar limit.",
     [2], [1], True),
    ("I've set up watch #4 and will tell you once.", [4], [], True),
    # a claim with nothing created this turn is the AGENT-08-style lie the step exists for
    ("I've set up watch #1 for that.", [], [1], False),
    ("Done, I'll let you know.", [], [], False),
    # a quoted id nobody created
    ("I've set up watch #9.", [4], [], False),
    # claims creation but quotes only an old id while a new one exists
    ("I've set up watch #1 for that.", [5], [1], False),
    # no claim, no ids
    ("There are 1,265 open invoices right now.", [], [], True),
    # recalled id without any claim
    ("You already have watch #1 on this.", [], [1], True),
])
def test_reply_truthful(reply, created, history, want):
    ok, cited = sc.reply_truthful(reply, created, history)
    assert ok is want
    assert cited == [int(x) for x in re.findall(r"#(\d+)", reply)]


def test_reply_truthful_empty_reply():
    assert sc.reply_truthful(None, [], []) == (True, [])


def test_run_digest_names_tools_refusals_and_reply():
    tools = [{"tool": "propose_kpi",
              "result_digest": "REFUSED: already proposed or declined (action #7, rejected)"}]
    out = sc.run_digest(tools, {"status": "done", "result_summary": "That was declined\nearlier."})
    assert "status=done" in out and "propose_kpi: REFUSED: already proposed" in out
    assert "That was declined earlier." in out
    assert "no tool calls" in sc.run_digest([], {"status": "failed"})
    assert "no tool calls" in sc.run_digest(None, None)


def test_fresh_thread_clears_every_invoice_titled_kpi_proposal():
    src = SCRIPT.read_text(encoding="utf-8")
    sql = src[src.index("def _fresh_thread"):src.index("def main")]
    # normalized-title match anywhere, not the old 'open invoice%' prefix
    assert "ILIKE 'open invoice%'" not in sql
    assert "regexp_replace(lower(payload->>'title'), '[^[:alnum:]]+', ' ', 'g')" in sql
    assert "LIKE '%invoice%'" in sql
    # only rejected rows, or this scenario's own pending chat proposals on ui:owner
    assert "status = 'rejected'" in sql
    assert "payload->>'thread_id' = 'ui:owner'" in sql and "payload->>'origin' = 'chat'" in sql
    assert "'executed'" not in sql
