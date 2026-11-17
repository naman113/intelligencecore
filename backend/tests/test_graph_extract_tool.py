"""extract_document (agents/graph.py) — pins that the tool goes through
docintel.extract_or_reuse (the sha-dedupe front door), NEVER the raw
extract_pdf + store_document pair, and that it surfaces the reused flag,
document id, and the ACTUAL line items (a bare count under "line_items" once
starved the document agent of ground truth and it fabricated an invoice).
Pure logic: docintel and the tool-call log are faked; no DB/LLM/network
(langchain/langgraph import for real, as in test_prompt_layout)."""

import json

from app import docintel
from app.agents import graph

LINE_ITEMS = [
    {"description": "Server rack 42U", "qty": 2, "unit_price": 50000.0,
     "amount": 100000.0},
    {"description": "Installation service", "qty": 1, "unit_price": 18000.0,
     "amount": 18000.0},
]


async def _noop_log(name, args, result, start):
    pass


async def test_extract_document_uses_extract_or_reuse(monkeypatch, tmp_path):
    f = tmp_path / "invoice.pdf"
    f.write_bytes(b"%PDF-1.4 tiny")
    monkeypatch.setattr(graph, "_work_path", lambda filename: (tmp_path, f))
    monkeypatch.setattr(graph, "_log_tool", _noop_log)

    calls = []

    async def fake_reuse(path, *, source, thread_id, task_run_id):
        calls.append({"path": path, "source": source,
                      "thread_id": thread_id, "task_run_id": task_run_id})
        return 7, {"doc_type": "invoice", "title": "T", "summary": "S",
                   "fields": {}, "line_items": list(LINE_ITEMS)}, True
    monkeypatch.setattr(docintel, "extract_or_reuse", fake_reuse)

    proposed = []

    async def fake_propose(doc_id, extracted, task_run_id=None):
        proposed.append((doc_id, task_run_id))
        return 42
    monkeypatch.setattr(docintel, "propose_record", fake_propose)

    async def _forbidden(*a, **kw):
        raise AssertionError(
            "extract_document must go through extract_or_reuse, not "
            "extract_pdf/store_document — that is what dedupes repeat sends")
    monkeypatch.setattr(docintel, "extract_pdf", _forbidden)
    monkeypatch.setattr(docintel, "store_document", _forbidden)

    fn = graph.extract_document.coroutine or graph.extract_document.func
    tok_run = graph.ctx_run_id.set(55)
    tok_phone = graph.ctx_phone.set("911234567890")
    try:
        result = await fn("invoice.pdf")
    finally:
        graph.ctx_run_id.reset(tok_run)
        graph.ctx_phone.reset(tok_phone)

    assert len(calls) == 1, "exactly one extract_or_reuse call"
    assert calls[0]["path"] == f
    assert calls[0]["source"] == "whatsapp"       # phone thread → whatsapp intake
    assert calls[0]["thread_id"] == "911234567890"
    assert calls[0]["task_run_id"] == 55

    out = json.loads(result)                       # a TOOL ERROR would fail here
    assert out["reused"] is True
    assert out["document_id"] == 7
    assert out["proposed_action_id"] == 42
    assert proposed == [(7, 55)], "the approval action hangs on the reused row"
    # Ground truth, not a summary of it: the ACTUAL line items ride in the
    # result. A bare integer under "line_items" told the model "there are 4
    # line items" while withholding what they were — and a downstream agent
    # invented them.
    assert out["line_items"] == LINE_ITEMS
    assert out["line_item_count"] == 2
