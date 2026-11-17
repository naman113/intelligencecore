"""get_document (agents/graph.py) — the delegation-by-reference fix.

Incident pin: the supervisor delegated "based on the extracted data from
document_id 6" to a document agent that had NO way to dereference the id, so
it fabricated an entire invoice. get_document is the dereference: it returns
the FULL stored extraction — every field value WITH its source quote (the
grounding evidence) and the complete line-item list — or an explicit ERROR
for unknown ids. Pure logic: docintel.get_extraction and _log_tool are faked;
no DB/LLM/network (as in test_graph_extract_tool)."""

import json

from app import docintel, playbook_tools
from app.agents import graph

ROW = {
    "id": 6,
    "filename": "invoice_tc006.jpg",
    "doc_type": "invoice",
    "status": "extracted",
    "summary": "Invoice TC-006 from Northwind Solutions",
    "extracted": {
        "doc_type": "invoice",
        "title": "Invoice TC-006",
        "summary": "Invoice TC-006 from Northwind Solutions",
        "fields": {
            "invoice_number": {"value": "TC-006", "confidence": 0.98,
                               "quote": "Invoice No: TC-006"},
            "grand_total": {"value": "118000.00", "confidence": 0.61,
                            "quote": "Grand Total 118,000.00"},
        },
        "line_items": [
            {"description": "Server rack 42U", "qty": 2,
             "unit_price": 50000.0, "amount": 100000.0},
            {"description": "Installation service", "qty": 1,
             "unit_price": 18000.0, "amount": 18000.0},
        ],
    },
}


def _capture_log(monkeypatch):
    calls = []

    async def fake_log(name, args, result, start, refused=False):
        calls.append({"name": name, "args": args, "result": result})

    monkeypatch.setattr(graph, "_log_tool", fake_log)
    return calls


async def _call(document_id):
    fn = graph.get_document.coroutine or graph.get_document.func
    tok = graph.ctx_run_id.set(77)
    try:
        return await fn(document_id=document_id)
    finally:
        graph.ctx_run_id.reset(tok)


# ── happy path ───────────────────────────────────────────────────────────

async def test_get_document_returns_full_extraction(monkeypatch):
    calls = _capture_log(monkeypatch)
    seen = []

    async def fake_get(document_id):
        seen.append(document_id)
        return json.loads(json.dumps(ROW))  # fresh copy each call

    monkeypatch.setattr(docintel, "get_extraction", fake_get)

    result = await _call(6)

    assert seen == [6]
    out = json.loads(result)                # a TOOL ERROR would fail here
    assert out["document_id"] == 6
    assert out["filename"] == "invoice_tc006.jpg"
    assert out["doc_type"] == "invoice"
    assert out["status"] == "extracted"
    assert out["title"] == "Invoice TC-006"
    assert out["summary"] == "Invoice TC-006 from Northwind Solutions"
    # Confidence dropped (noise to a renderer); quote KEPT — it is the evidence
    # tying each value back to the original document. Exact equality pins both.
    assert out["fields"] == {
        "invoice_number": {"value": "TC-006", "quote": "Invoice No: TC-006"},
        "grand_total": {"value": "118000.00", "quote": "Grand Total 118,000.00"},
    }
    # The FULL line-item list, not a count — a count is what starved the agent.
    assert out["line_items"] == ROW["extracted"]["line_items"]
    assert calls == [{"name": "get_document", "args": {"document_id": 6},
                      "result": result}], \
        "exactly one _log_tool call, filed under the tool name"


# ── error paths ──────────────────────────────────────────────────────────

async def test_get_document_unknown_id_exact_error(monkeypatch):
    calls = _capture_log(monkeypatch)

    async def fake_get(document_id):
        return None

    monkeypatch.setattr(docintel, "get_extraction", fake_get)

    result = await _call(99)

    # EXACT string: the model must be redirected to search_documents, never
    # left to improvise a document that does not exist.
    assert result == "ERROR: document 99 not found — use search_documents"
    assert len(calls) == 1, "the failed lookup still lands in the audit log"
    assert calls[0]["result"] == result


async def test_get_document_unexpected_exception_is_tool_error(monkeypatch):
    calls = _capture_log(monkeypatch)

    async def fake_get(document_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(docintel, "get_extraction", fake_get)

    result = await _call(6)

    assert result == "TOOL ERROR: RuntimeError: db down"
    assert len(calls) == 1


async def test_get_document_result_capped_at_20k(monkeypatch):
    _capture_log(monkeypatch)
    huge = json.loads(json.dumps(ROW))
    huge["extracted"]["fields"] = {
        f"f{i}": {"value": "v" * 2000, "confidence": 1.0, "quote": "q"}
        for i in range(30)}

    async def fake_get(document_id):
        return huge

    monkeypatch.setattr(docintel, "get_extraction", fake_get)

    result = await _call(6)

    assert len(result) == 20000


# ── registration pins ────────────────────────────────────────────────────

def test_get_document_registered():
    assert "get_document" in graph._tool_registry()


def test_document_superset_is_exactly_the_contract_list():
    assert graph.AGENT_TOOL_SUPERSET["document"] == [
        "create_pdf", "create_docx", "create_xlsx", "create_markdown",
        "get_document", "search_documents",
        "run_python", "list_artifacts", "send_document",
        "use_skill", "skill_asset",   # Agent Skills (test_skills_tools.py)
    ]


def test_supervisor_superset_gains_get_document_after_search_documents():
    sup = graph.AGENT_TOOL_SUPERSET["supervisor"]
    assert "get_document" in sup
    assert sup.index("get_document") == sup.index("search_documents") + 1


def test_get_document_not_leaked_into_other_agents():
    for agent, tools in graph.AGENT_TOOL_SUPERSET.items():
        if agent in ("document", "supervisor"):
            continue
        assert "get_document" not in tools, \
            f"agent '{agent}' must not gain get_document"


def test_playbook_tiers_untouched_by_get_document():
    for tier, caps in playbook_tools.TIER_CAPABILITIES.items():
        assert "get_document" not in caps, \
            f"tier '{tier}' must not gain a get_document capability"


# ── search_documents carries line items too (same starvation class) ─────

class _DocPool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, sql, *args):
        return self.rows


async def test_search_documents_hits_carry_line_items_capped_at_20(monkeypatch):
    items = [{"description": f"item {i}", "qty": 1,
              "unit_price": 10.0, "amount": 10.0} for i in range(25)]
    row = {"id": 6, "filename": "invoice_tc006.jpg", "source": "whatsapp",
           "doc_type": "invoice", "summary": "an invoice",
           "status": "extracted", "created_at": "2026-01-01",
           "extracted": json.dumps({"fields": {"total": {"value": "1"}},
                                    "line_items": items})}

    async def fake_pool():
        return _DocPool([row])

    monkeypatch.setattr(graph, "get_pool", fake_pool)
    result = await graph.search_documents.ainvoke({"query": "invoice"})
    docs = json.loads(result)
    assert docs[0]["line_items"] == items[:20], \
        "each hit carries the actual line items (first 20), not a count"


async def test_search_documents_corrupt_row_does_not_poison_other_hits(monkeypatch):
    """docintel only setdefaults line_items — an extraction model that emitted
    a scalar/string/invalid value persists as-is. One such row must degrade to
    empty fields/line_items, not turn the WHOLE tool result into TOOL ERROR
    (losing every good hit) or explode a string into per-character garbage."""
    items = [{"description": "good item", "qty": 1,
              "unit_price": 10.0, "amount": 10.0}]

    def _row(doc_id, extracted):
        return {"id": doc_id, "filename": f"doc{doc_id}.pdf", "source": "whatsapp",
                "doc_type": "invoice", "summary": "an invoice",
                "status": "extracted", "created_at": "2026-01-01",
                "extracted": extracted}

    rows = [
        _row(6, json.dumps({"fields": {"total": {"value": "1"}},
                            "line_items": items})),
        _row(7, json.dumps({"fields": {}, "line_items": 5})),        # scalar
        _row(8, json.dumps({"fields": {}, "line_items": "none"})),   # string
        _row(9, "{not json"),                                        # corrupt
    ]

    async def fake_pool():
        return _DocPool(rows)

    monkeypatch.setattr(graph, "get_pool", fake_pool)
    result = await graph.search_documents.ainvoke({"query": "invoice"})
    docs = json.loads(result)                # a TOOL ERROR would fail here
    assert [d["document_id"] for d in docs] == [6, 7, 8, 9], \
        "every hit survives, including the corrupt ones"
    assert docs[0]["line_items"] == items, "the good row keeps its data"
    assert docs[1]["line_items"] == [], "scalar line_items degrades to []"
    assert docs[2]["line_items"] == [], \
        "string line_items degrades to [] — never a per-character list"
    assert docs[3]["line_items"] == [] and docs[3]["fields"] == {}


# ── docintel.get_extraction — the jsonb-as-string decode (repo invariant) ─
# asyncpg returns jsonb as a STRING without a registered codec; every
# get_document test above monkeypatches get_extraction, so these run the real
# helper against a fake pool. Removing the json.loads turns every production
# get_document call into 'TOOL ERROR: AttributeError: ...' while the
# monkeypatched tests stay green — exactly the silent regression to pin.

class _OneRowPool:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, sql, *args):
        return self.row if args and args[0] == self.row["id"] else None


def _db_row(extracted):
    return {"id": 6, "filename": "invoice_tc006.jpg", "doc_type": "invoice",
            "status": "extracted",
            "summary": "Invoice TC-006 from Northwind Solutions",
            "extracted": extracted}


async def test_get_extraction_decodes_jsonb_string(monkeypatch):
    row = _db_row(json.dumps(ROW["extracted"]))     # what asyncpg hands back

    async def fake_pool():
        return _OneRowPool(row)

    monkeypatch.setattr(docintel, "get_pool", fake_pool)
    out = await docintel.get_extraction(6)
    assert out["extracted"] == ROW["extracted"], \
        "a jsonb string column must come back as the decoded dict"
    assert isinstance(out["extracted"], dict)
    assert out["filename"] == "invoice_tc006.jpg"


async def test_get_extraction_tolerates_none_and_dict(monkeypatch):
    for stored, want in ((None, {}),
                         (dict(ROW["extracted"]), ROW["extracted"])):
        async def fake_pool(_row=_db_row(stored)):
            return _OneRowPool(_row)

        monkeypatch.setattr(docintel, "get_pool", fake_pool)
        out = await docintel.get_extraction(6)
        assert out["extracted"] == want


async def test_get_extraction_unknown_id_returns_none(monkeypatch):
    async def fake_pool():
        return _OneRowPool(_db_row("{}"))

    monkeypatch.setattr(docintel, "get_pool", fake_pool)
    assert await docintel.get_extraction(99) is None


async def test_get_document_end_to_end_decodes_string_extraction(monkeypatch):
    """get_document through the REAL get_extraction, with the DB returning the
    jsonb column as a string — the whole dereference path, no monkeypatched
    seam in the middle."""
    _capture_log(monkeypatch)
    row = _db_row(json.dumps(ROW["extracted"]))

    async def fake_pool():
        return _OneRowPool(row)

    monkeypatch.setattr(docintel, "get_pool", fake_pool)
    result = await _call(6)
    out = json.loads(result)                # a TOOL ERROR would fail here
    assert out["fields"]["grand_total"] == {
        "value": "118000.00", "quote": "Grand Total 118,000.00"}
    assert out["line_items"] == ROW["extracted"]["line_items"]
