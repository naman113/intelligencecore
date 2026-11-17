"""Phase 16 plan 08: 20-question comparison of governed measures and real text-to-SQL.

DEMO STACKS ONLY. Every question is answered up to three ways: hand-written reference SQL
(ground truth), the measure lane (query_measure in the app container) and the raw lane (the
agent with the data-modeling skill disabled and the measure tools and the two-lanes prompt
line removed from the supervisor and data configs; configs restored in a finally block).
A skill lane then checks that, with the skill enabled, the agent routes through measures.

Run from backend/:
  SCENARIO_DEMO_STACK=1 STACK=p26 uv run --frozen python scripts/model_compare.py \\
      --secrets <main secrets.env> [--llm-secrets <phase secrets.env>] [--no-llm] [--skill-only] [--out <json>]

Exit codes: 0 pass, 1 mismatch / contamination / config not restored, 2 refused.
Secrets are never printed.
"""

import importlib.util
import json
import os
import random
import re
import string
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("scenario_model_for_compare", HERE / "scenario_model.py")
sm = importlib.util.module_from_spec(_spec)
sys.modules["scenario_model_for_compare"] = sm
_spec.loader.exec_module(sm)

MEASURE_TOOLS = ("get_model", "query_measure", "propose_model_change", "propose_relationships_from_keys")
MARKER = "TWO LANES (Phase 16)"
SEVEN = ("order_count", "net_sales", "invoiced_amount", "collected_amount", "stock_on_hand",
         "average_order_value", "net_sales_mom_growth")
SKILL = "data-modeling"
TOL = 0.005
ASK_TIMEOUT = 600

# ------------------------------------------------------------------ question set

_NS = "SUM(l.qty * l.unit_price * (1 - l.discount_pct / 100))"
_OK = "o.status NOT IN ('cancelled', 'returned')"
_NC = "o.status <> 'cancelled'"


def _rng(col, a, b):
    return f"{col} >= DATE '{a}' AND {col} < DATE '{b}'"


def _ns_from(extra_join=""):
    return "FROM order_lines l JOIN sales_orders o ON o.id = l.order_id " + extra_join


def _r(start, end, **kw):
    d = {"range": {"start": start, "end": end}}
    d.update(kw)
    return d


def _q(qid, question, expected, reqs, sql, compare="scalar", percent=False):
    return {"id": qid, "question": question, "expected": expected, "measure_requests": reqs,
            "reference_sql": sql, "compare": compare, "percent": percent}


_REGION_JOIN = "JOIN customers c ON c.id = o.customer_id JOIN regions r ON r.id = c.region_id "
_CUST_JOIN = "JOIN customers c ON c.id = o.customer_id "
_PROD_JOIN = "JOIN products p ON p.id = l.product_id "
_SEP = ("2026-09-01", "2026-10-01")
_Q3 = ("2026-07-01", "2026-10-01")

QUESTIONS = [
    _q("q01", "What were our net sales in September 2026?", "modeled",
       [_r(*_SEP, measure="net_sales")],
       f"SELECT {_NS} AS value {_ns_from()}WHERE {_OK} AND {_rng('o.order_date', *_SEP)}"),
    _q("q02", "What were net sales by region for Q3 2026 (July to September)?", "modeled",
       [_r(*_Q3, measure="net_sales", by=["public.regions.name"])],
       f"SELECT r.name AS k, {_NS} AS value {_ns_from(_REGION_JOIN)}WHERE {_OK} AND "
       f"{_rng('o.order_date', *_Q3)} GROUP BY r.name", "rows"),
    _q("q03", "How many orders did we take from 2026-10-16 to 2026-10-22 inclusive?", "modeled",
       [_r("2026-10-16", "2026-10-23", measure="order_count")],
       f"SELECT COUNT(*) AS value FROM sales_orders o WHERE {_NC} AND "
       f"{_rng('o.order_date', '2026-10-16', '2026-10-23')}"),
    _q("q04", "What was the average order value in September 2026?", "modeled",
       [_r(*_SEP, measure="average_order_value")],
       f"SELECT (SELECT {_NS} {_ns_from()}WHERE {_OK} AND {_rng('o.order_date', *_SEP)}) / "
       f"(SELECT COUNT(*) FROM sales_orders o WHERE {_NC} AND {_rng('o.order_date', *_SEP)}) AS value"),
    _q("q05", "What was the average order value by customer segment for Q3 2026?", "modeled",
       [_r(*_Q3, measure="average_order_value", by=["public.customers.segment"])],
       f"SELECT n.k AS k, n.v / d.c AS value FROM "
       f"(SELECT c.segment AS k, {_NS} AS v {_ns_from(_CUST_JOIN)}"
       f"WHERE {_OK} AND {_rng('o.order_date', *_Q3)} GROUP BY c.segment) n JOIN "
       f"(SELECT c.segment AS k, COUNT(*) AS c FROM sales_orders o {_CUST_JOIN}"
       f"WHERE {_NC} AND {_rng('o.order_date', *_Q3)} GROUP BY c.segment) d ON d.k = n.k", "rows"),
    _q("q06", "How did net sales in October 2026 (to date) change against September, month over month?",
       "modeled",
       [_r("2026-10-01", "2026-11-01", measure="net_sales_mom_growth", grain="month")],
       f"SELECT (oc.v - sp.v) / NULLIF(sp.v, 0) AS value FROM "
       f"(SELECT {_NS} AS v {_ns_from()}WHERE {_OK} AND {_rng('o.order_date', '2026-10-01', '2026-11-01')}) oc, "
       f"(SELECT {_NS} AS v {_ns_from()}WHERE {_OK} AND {_rng('o.order_date', *_SEP)}) sp",
       percent=True),
    _q("q07", "What were South region net sales from 2026-08-01 to 2026-10-22 inclusive, against the same "
              "dates a year earlier (2025-08-01 to 2025-10-22)?", "modeled",
       [_r("2026-08-01", "2026-10-23", measure="net_sales", key="current",
           filters=[{"dimension": "public.regions.name", "op": "in", "values": ["South"]}]),
        _r("2025-08-01", "2025-10-23", measure="net_sales", key="prior",
           filters=[{"dimension": "public.regions.name", "op": "in", "values": ["South"]}])],
       f"SELECT 'current' AS k, {_NS} AS value {_ns_from(_REGION_JOIN)}WHERE r.name = 'South' AND {_OK} AND "
       f"{_rng('o.order_date', '2026-08-01', '2026-10-23')} UNION ALL "
       f"SELECT 'prior' AS k, {_NS} AS value {_ns_from(_REGION_JOIN)}WHERE r.name = 'South' AND {_OK} AND "
       f"{_rng('o.order_date', '2025-08-01', '2025-10-23')}", "rows"),
    _q("q08", "What were Beverages net sales in the six months before 2026-03-01 (from 2025-09-01) and in "
              "the six months after (to 2026-08-31)?", "modeled",
       [_r("2025-09-01", "2026-03-01", measure="net_sales", key="before",
           filters=[{"dimension": "public.products.category", "op": "in", "values": ["Beverages"]}]),
        _r("2026-03-01", "2026-09-01", measure="net_sales", key="after",
           filters=[{"dimension": "public.products.category", "op": "in", "values": ["Beverages"]}])],
       f"SELECT 'before' AS k, {_NS} AS value {_ns_from(_PROD_JOIN)}"
       f"WHERE p.category = 'Beverages' AND {_OK} AND {_rng('o.order_date', '2025-09-01', '2026-03-01')} "
       f"UNION ALL SELECT 'after' AS k, {_NS} AS value {_ns_from(_PROD_JOIN)}"
       f"WHERE p.category = 'Beverages' AND {_OK} AND {_rng('o.order_date', '2026-03-01', '2026-09-01')}",
       "rows"),
    _q("q09", "What is our total stock on hand at the latest inventory snapshot?", "modeled",
       [{"measure": "stock_on_hand"}],
       "SELECT SUM(on_hand) AS value FROM inventory_snapshots WHERE snapshot_date = "
       "(SELECT MAX(snapshot_date) FROM inventory_snapshots)"),
    _q("q10", "What is the stock on hand at the latest snapshot for SKU-0005 and SKU-0042?", "modeled",
       [{"measure": "stock_on_hand", "by": ["public.products.sku"],
         "filters": [{"dimension": "public.products.sku", "op": "in", "values": ["SKU-0005", "SKU-0042"]}]}],
       "SELECT p.sku AS k, s.on_hand AS value FROM inventory_snapshots s JOIN products p ON p.id = s.product_id "
       "WHERE p.sku IN ('SKU-0005', 'SKU-0042') AND s.snapshot_date = "
       "(SELECT MAX(snapshot_date) FROM inventory_snapshots)", "rows"),
    _q("q11", "How much did we invoice in September 2026?", "modeled",
       [_r(*_SEP, measure="invoiced_amount")],
       f"SELECT SUM(amount) AS value FROM invoices WHERE {_rng('invoice_date', *_SEP)}"),
    _q("q12", "How much did we collect in payments in September 2026?", "modeled",
       [_r(*_SEP, measure="collected_amount")],
       f"SELECT SUM(amount) AS value FROM payments WHERE {_rng('paid_at', *_SEP)}"),
    _q("q13", "Who are our top 5 customers by net sales in 2026 (to 2026-10-22)?", "modeled",
       [_r("2026-01-01", "2026-10-23", measure="net_sales", by=["public.customers.name"], top=5)],
       f"SELECT c.name AS k, {_NS} AS value {_ns_from(_CUST_JOIN)}"
       f"WHERE {_OK} AND {_rng('o.order_date', '2026-01-01', '2026-10-23')} GROUP BY c.name "
       f"ORDER BY value DESC LIMIT 5", "rows"),
    _q("q14", "What were net sales by sales channel in September 2026?", "modeled",
       [_r(*_SEP, measure="net_sales", by=["public.sales_orders.channel"])],
       f"SELECT o.channel AS k, {_NS} AS value {_ns_from()}WHERE {_OK} AND {_rng('o.order_date', *_SEP)} "
       f"GROUP BY o.channel", "rows"),
    _q("q15", "How many orders did each sales rep's customers place in September 2026?", "modeled",
       [_r(*_SEP, measure="order_count", by=["public.sales_reps.full_name"])],
       f"SELECT r.full_name AS k, COUNT(*) AS value FROM sales_orders o {_CUST_JOIN}"
       f"LEFT JOIN sales_reps r ON r.id = c.rep_id WHERE {_NC} AND {_rng('o.order_date', *_SEP)} "
       f"GROUP BY r.full_name", "rows"),
    _q("q16", "What is the average number of days to pay for Hospitality customers' invoices dated since "
              "2026-06-01?", "not_modeled",
       [_r("2026-06-01", "2026-10-23", measure="days_to_pay")],
       "SELECT AVG(p.paid_at - i.invoice_date) AS value FROM payments p JOIN invoices i ON i.id = p.invoice_id "
       "JOIN customers c ON c.id = i.customer_id WHERE c.segment = 'Hospitality' "
       "AND i.invoice_date >= DATE '2026-06-01'"),
    _q("q17", "As of 2026-10-23, how many invoices dated more than 90 days earlier are still unpaid?",
       "not_modeled", [{"measure": "unpaid_invoice_amount"}],
       "SELECT COUNT(*) AS value FROM invoices i WHERE i.invoice_date < DATE '2026-07-25' "
       "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.invoice_id = i.id)"),
    _q("q18", "How many distinct customers placed an order in September 2026?", "not_modeled",
       [_r(*_SEP, measure="active_customers")],
       f"SELECT COUNT(DISTINCT o.customer_id) AS value FROM sales_orders o WHERE {_NC} AND "
       f"{_rng('o.order_date', *_SEP)}"),
    _q("q19", "What was the return rate in Q3 2026: returned orders as a share of non-cancelled orders?",
       "not_modeled", [_r(*_Q3, measure="return_rate")],
       f"SELECT SUM(CASE WHEN o.status = 'returned' THEN 1 ELSE 0 END)::numeric / "
       f"NULLIF(SUM(CASE WHEN o.status <> 'cancelled' THEN 1 ELSE 0 END), 0) AS value "
       f"FROM sales_orders o WHERE {_rng('o.order_date', *_Q3)}", percent=True),
    _q("q20", "How many units did we sell by product category in September 2026 (excluding cancelled and "
              "returned orders)?", "not_modeled",
       [_r(*_SEP, measure="units_sold", by=["public.products.category"])],
       f"SELECT p.category AS k, SUM(l.qty) AS value {_ns_from(_PROD_JOIN)}"
       f"WHERE {_OK} AND {_rng('o.order_date', *_SEP)} GROUP BY p.category", "rows"),
]


# ------------------------------------------------------------------ pure helpers

_NUM = re.compile(r"(?<![\w#.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w#.])-?\d+(?:\.\d+)?")


def extract_numbers(text: str) -> list:
    """Numbers in a reply, thousands commas removed; '#431' style ids are ignored."""
    cleaned = re.sub(r"(?:action|case|run|card|task)?\s*#\d+", " ", text or "", flags=re.I)
    out = []
    for m in _NUM.finditer(cleaned):
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            continue
    return out


def agrees(reference, candidate, tol: float = TOL) -> bool:
    """Scalars: relative tolerance (absolute 0.01 when the reference is 0).
    Dicts (rows keyed by dimension value): same key set, each value within tolerance."""
    if isinstance(reference, dict) or isinstance(candidate, dict):
        if not (isinstance(reference, dict) and isinstance(candidate, dict)):
            return False
        if set(reference) != set(candidate):
            return False
        return all(agrees(reference[k], candidate[k], tol) for k in reference)
    try:
        r, c = float(reference), float(candidate)
    except (TypeError, ValueError):
        return False
    if r == 0:
        return abs(c) <= 0.01
    return abs(c - r) / abs(r) <= tol


def _num_matches(ref, numbers, percent=False) -> bool:
    for n in numbers:
        if agrees(ref, n) or (percent and agrees(ref * 100, n)):
            return True
    return False


def score_raw(reply, reference, percent: bool = False) -> str:
    """'agree' | 'wrong' | 'no_answer' for a raw-lane reply against the reference."""
    numbers = extract_numbers(reply)
    if not numbers:
        return "no_answer"
    if isinstance(reference, dict):
        if not reference:
            return "wrong"
        hit = sum(1 for v in reference.values() if _num_matches(v, numbers, percent))
        return "agree" if hit / len(reference) >= 0.8 else "wrong"
    return "agree" if _num_matches(reference, numbers, percent) else "wrong"


def strip_phase16_prompt(prompt: str) -> str:
    """Remove the Phase 16 two-lanes text: a whole line, or the tail of the last line."""
    if MARKER not in (prompt or ""):
        return prompt
    out = []
    for line in prompt.split("\n"):
        if MARKER not in line:
            out.append(line)
        elif line.lstrip().startswith("- " + MARKER):
            continue
        else:
            out.append(line[:line.index(MARKER)].rstrip())
    return "\n".join(out)


def raw_lane_body(entry: dict) -> dict:
    cfg = entry["config"]
    body = {"prompt": strip_phase16_prompt(cfg["prompt"]), "model": cfg.get("model", "auto"),
            "temperature": cfg.get("temperature", 0.2), "max_tokens": cfg.get("max_tokens", 4096),
            "recursion_limit": cfg.get("recursion_limit", 25),
            "tools": [t for t in cfg.get("tools", []) if t not in MEASURE_TOOLS]}
    if any(t in MEASURE_TOOLS for t in body["tools"]) or len(body["prompt"]) < 20:
        raise ValueError("raw lane body still has a measure tool or the prompt is too short")
    return body


def restore_plan(entry: dict):
    if not entry.get("customized"):
        return ("delete", None)
    cfg = entry["config"]
    return ("put", {"prompt": cfg["prompt"], "model": cfg.get("model", "auto"),
                    "temperature": cfg.get("temperature", 0.2),
                    "max_tokens": cfg.get("max_tokens", 4096),
                    "recursion_limit": cfg.get("recursion_limit", 25),
                    "tools": list(cfg.get("tools", []))})


def raw_verdict(tool_names, reply, reference, compare="scalar", percent: bool = False) -> str:
    names = [str(n).removesuffix(":refused") for n in tool_names]
    if any(n in MEASURE_TOOLS for n in names):
        return "contaminated"
    if "run_sql" not in names:
        return "no_sql"
    return score_raw(reply, reference, percent)


def summarise(results: list) -> dict:
    t = dict.fromkeys(("measure_agree", "measure_mismatch", "not_modeled_correct", "not_modeled_wrong",
                       "raw_agree", "raw_wrong", "raw_no_answer", "raw_no_sql", "raw_contaminated",
                       "raw_skipped"), 0)
    for r in results:
        t[r["measure_status"]] += 1
        t["raw_" + r.get("raw_verdict", "skipped")] += 1
    return t


def _norm_key(v) -> str:
    s = str(v)
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else s


def reference_value(rows: list, compare: str):
    """Reference rows -> float (scalar) or {key: float} (rows)."""
    if compare == "scalar":
        v = rows[0].get("value") if rows else None
        return None if v is None else float(v)
    return {_norm_key(r["k"]): float(r["value"]) for r in rows if r.get("value") is not None}


def measure_value(doc: dict, compare: str, top=None):
    """query_measure JSON -> float or {key: float}; the value column is named like the measure."""
    cols, rows = doc.get("columns") or [], doc.get("rows") or []
    vcol = doc.get("measure") if doc.get("measure") in cols else (cols[-1] if cols else "value")
    if compare == "scalar":
        if not rows:
            return None
        v = rows[-1].get(vcol)
        return None if v is None else float(v)
    keys = [c for c in cols if c != vcol]
    pairs = {}
    for r in rows:
        if r.get(vcol) is None:
            continue
        pairs["|".join(_norm_key(r.get(c)) for c in keys)] = float(r[vcol])
    if top:
        pairs = dict(sorted(pairs.items(), key=lambda kv: -kv[1])[:top])
    return pairs


# ------------------------------------------------------------------ flow

class Compare(sm.Scenario):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.tag = "".join(random.choice(string.ascii_lowercase) for _ in range(5))

    def agents(self):
        return {e["config_key"]: e for e in self.get("/api/agents")
                if e.get("config_key") in ("supervisor", "data")}

    def ensure_model(self, secrets):
        text = self.tool("get_model", {})
        missing = [m for m in SEVEN if m not in str(text)]
        if missing:
            env = dict(os.environ, SCENARIO_DEMO_STACK="1", STACK=self.c["app"].split("-")[1])
            r = subprocess.run([sys.executable, str(HERE / "scenario_model.py"), "--tools-only",
                                "--secrets", secrets], env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            print(f"model re-created ({len(missing)} measures were missing): rc={r.returncode}", flush=True)

    def batch(self, calls):
        """Run many ('sql', text) / ('tool', name, args) calls in ONE app-container process."""
        return self.in_app(
            "[await (guardrails.run_readonly_sql(c[1]) if c[0] == 'sql' "
            "else getattr(mt, c[1]).ainvoke(c[2])) for c in P['calls']]",
            {"calls": [list(c) for c in calls]})

    def ask_wait(self, operator, text):
        _, b = self.call("POST", "/api/ask", {"message": text, "operator": operator})
        run_id, mid = b["run_id"], b["message_id"]
        end = time.time() + ASK_TIMEOUT
        while time.time() < end:
            time.sleep(4)
            t = self.get(f"/api/ask?operator={operator}&limit=20")
            if not t.get("pending"):
                reply = "\n".join(str(m["content"]) for m in t["messages"]
                                  if m["role"] == "assistant" and m["id"] > mid)
                return run_id, reply
        return run_id, ""

    def run_tools(self, run_id):
        calls = self.get(f"/api/runs/{run_id}/tools")
        return [c["tool"] for c in calls], calls

    def ask_question(self, operator, q, ref):
        run_id, reply = self.ask_wait(operator, q["question"])
        names, calls = self.run_tools(run_id)
        sqls = []
        for c in calls:
            if c["tool"] == "run_sql":
                a = c.get("args")
                a = json.loads(a) if isinstance(a, str) else (a or {})
                sqls.append(str(a.get("sql") or a.get("query") or a)[:600])
        return {"verdict": raw_verdict(names, reply, ref, q["compare"], q["percent"]),
                "tools": names, "sql": sqls, "reply": reply[:500]}


def run_reference_and_measure(s, results):
    calls, plan = [], []
    for q in QUESTIONS:
        plan.append((len(calls), len(q["measure_requests"])))
        calls.append(("sql", q["reference_sql"]))
        for req in q["measure_requests"]:
            calls.append(("tool", "query_measure", {k: v for k, v in req.items() if k not in ("key", "top")}))
    out = s.batch(calls)
    for q, (at, n) in zip(QUESTIONS, plan):
        ref = reference_value(out[at], q["compare"])
        answers, unmodeled = {}, False
        for req, text in zip(q["measure_requests"], out[at + 1: at + 1 + n]):
            if isinstance(text, str) and text.startswith(("NOT MODELED", "REFUSED")):
                unmodeled = True
                answers[req.get("key", "")] = text[:200]
                continue
            try:
                answers[req.get("key", "")] = measure_value(json.loads(text), "scalar" if n > 1 else q["compare"], req.get("top"))
            except (ValueError, TypeError):
                unmodeled = True
                answers[req.get("key", "")] = str(text)[:200]
        mval = answers if len(answers) > 1 else next(iter(answers.values()))
        if q["expected"] == "not_modeled":
            status = "not_modeled_correct" if unmodeled else "not_modeled_wrong"
        elif unmodeled:
            status = "measure_mismatch"
        else:
            status = ("measure_agree" if ref is not None and mval is not None and agrees(ref, mval)
                      else "measure_mismatch")
        results.append({"id": q["id"], "question": q["question"], "expected": q["expected"],
                        "reference": ref, "measure": mval, "measure_status": status,
                        "measures": sorted({r["measure"] for r in q["measure_requests"]}),
                        "raw_verdict": "skipped", "raw_tools": [], "raw_sql": []})


def raw_lane(s, results):
    saved = s.agents()
    if set(saved) != {"supervisor", "data"}:
        return "failed: supervisor and data entries not found", {"supervisor": False, "data": False}
    restored = {"supervisor": False, "data": False}
    status = "ran"
    try:
        s.call("PATCH", f"/api/skills/{SKILL}", {"enabled": False})
        for key, entry in saved.items():
            s.call("PUT", f"/api/agents/{key}/config", raw_lane_body(entry))
        for key, entry in s.agents().items():
            if (any(t in MEASURE_TOOLS for t in entry["config"]["tools"])
                    or MARKER in entry["config"]["prompt"]):
                raise sm.Fail(f"raw lane strip not in effect for {key}")
        for i, (q, res) in enumerate(zip(QUESTIONS, results)):
            op = f"cmp{s.tag}{chr(97 + i // 26)}{chr(97 + i % 26)}"
            try:
                out = s.ask_question(op, q, res["reference"])
            except Exception as e:  # noqa: BLE001
                out = {"verdict": "no_answer", "tools": [], "sql": [],
                       "reply": f"error {type(e).__name__}"}
            res["raw_verdict"], res["raw_tools"], res["raw_sql"] = out["verdict"], out["tools"], out["sql"]
            res["raw_reply"] = out["reply"]
            print(f"raw {q['id']}: {out['verdict']} tools={','.join(sorted(set(out['tools'])))}", flush=True)
    except Exception as e:  # noqa: BLE001
        status = f"failed: {type(e).__name__}: {str(e)[:200]}"
    finally:
        for key, entry in saved.items():
            try:
                kind, body = restore_plan(entry)
                if kind == "delete":
                    s.call("DELETE", f"/api/agents/{key}/config")
                else:
                    s.call("PUT", f"/api/agents/{key}/config", body)
            except Exception as e:  # noqa: BLE001
                print(f"restore {key} failed: {type(e).__name__}", flush=True)
        try:
            after = s.agents()
            for key, entry in saved.items():
                restored[key] = (after[key]["config"]["tools"] == entry["config"]["tools"]
                                 and after[key]["config"]["prompt"] == entry["config"]["prompt"])
        except Exception:  # noqa: BLE001
            pass
    return status, restored


LABEL_QUESTIONS = ("q17", "q19")


def label_ok(tool_names, reply) -> bool:
    """True when a raw run_sql number is labelled unverified (or no raw number was used and a
    measure was proposed / the reply says unverified)."""
    names = [str(n).split(":")[0] for n in (tool_names or [])]
    low = (reply or "").lower()
    if "run_sql" in names:
        return "unverified" in low
    return "unverified" in low or "propose_model_change" in names


def skill_ok(skill: dict) -> bool:
    if not skill.get("ran") or not skill.get("q01_used_query_measure"):
        return False
    return all((skill.get(q) or {}).get("label_ok") is True for q in LABEL_QUESTIONS)


def skill_lane(s):
    out = {"ran": True}
    try:
        s.call("PATCH", f"/api/skills/{SKILL}", {"enabled": True})
        rid, _ = s.ask_wait(f"cmp{s.tag}sa", QUESTIONS[0]["question"])
        names, _ = s.run_tools(rid)
        out["q01_tools"] = names
        out["q01_used_query_measure"] = "query_measure" in names
        for qid, suffix in zip(LABEL_QUESTIONS, ("sb", "sc")):
            text = next(q for q in QUESTIONS if q["id"] == qid)["question"]
            rid, reply = s.ask_wait(f"cmp{s.tag}{suffix}", text)
            names, _ = s.run_tools(rid)
            bare = [str(n).split(":")[0] for n in names]
            out[qid] = {"tools": names, "used_run_sql": "run_sql" in bare,
                        "used_get_model": "get_model" in bare,
                        "label_ok": label_ok(names, reply), "reply": reply[:600]}
    finally:
        try:
            s.call("PATCH", f"/api/skills/{SKILL}", {"enabled": False})
        except Exception:  # noqa: BLE001
            pass
    return out


def out_path_skill(argv) -> str:
    return sm._argval(argv, "--out") or "skill-lane.json"


def run(cfg, argv) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    s = Compare(cfg)
    secrets = sm._argval(argv, "--secrets")
    out_path = sm._argval(argv, "--out") or "16-08-compare.json"
    no_llm = "--no-llm" in argv or not sm.llm_keys_present(cfg["llm_secrets"])
    results = []
    s.ensure_model(secrets)
    if "--skill-only" in argv:
        if no_llm:
            skill = {"ran": False, "reason": "no LLM key or --no-llm"}
            print("skill lane: SKIP (no LLM key or --no-llm)", flush=True)
        else:
            skill = skill_lane(s)
            print("skill lane: ran=%s q01_used_query_measure=%s q17_label_ok=%s q19_label_ok=%s" % (
                skill.get("ran"), skill.get("q01_used_query_measure"),
                (skill.get("q17") or {}).get("label_ok"), (skill.get("q19") or {}).get("label_ok")),
                flush=True)
        Path(out_path_skill(argv)).write_text(
            json.dumps({"mode": "skill-only", "skill_lane": skill}, indent=2, default=str), encoding="utf-8")
        return 1 if (skill.get("ran") and not skill_ok(skill)) else 0
    run_reference_and_measure(s, results)
    for r in results:
        print(f"{r['id']}: ref={r['reference']} measure={r['measure_status']}", flush=True)
    restored = {"supervisor": None, "data": None}
    raw_status, skill = "skipped: no LLM key or --no-llm", {"ran": False, "reason": "no LLM key"}
    if not no_llm:
        raw_status, restored = raw_lane(s, results)
        if raw_status == "ran" and all(restored.values()):
            skill = skill_lane(s)
        else:
            skill = {"ran": False, "reason": "raw lane failed or configs not restored"}
    totals = summarise(results)
    doc = {"questions": results, "totals": totals, "restored": restored, "raw_lane": raw_status,
           "skill_lane": skill}
    Path(out_path).write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    print("compare: " + " ".join(f"{k}={v}" for k, v in totals.items()), flush=True)
    bad = bool(totals["measure_mismatch"] or totals["not_modeled_wrong"] or totals["raw_contaminated"]
               or (not no_llm and (raw_status != "ran" or not all(restored.values()))))
    if skill.get("ran") and not skill_ok(skill):
        print("skill lane: expectation not met (see json)", flush=True)
        bad = True
    return 1 if bad else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        cfg = sm.load_config(os.environ, argv)
    except ValueError as e:
        print(f"REFUSED: {e}")
        return 2
    why = sm.refusal(cfg, os.environ)
    if why:
        print(f"REFUSED: {why}")
        return 2
    return run(cfg, argv)


if __name__ == "__main__":
    sys.exit(main())
