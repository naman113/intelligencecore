"""Phase 16 live check: the governed semantic model on a demo stack.

DEMO STACKS ONLY. Twelve numbered checks drive the model_change approval path, the
compiled measures, the warehouse objects and the read-only Model API, then a restart and a
backup probe. Check 2 needs an LLM key (it is skipped without one).

Run from backend/:
  SCENARIO_DEMO_STACK=1 STACK=p26 uv run --frozen python scripts/scenario_model.py \\
      --secrets <main secrets.env> [--llm-secrets <phase secrets.env>] [--tools-only] [--agent-only]

Output: one "PASS <n> <text>", "FAIL <n> <text>: <detail>" or "SKIP <n> <text>: <reason>"
line per check. Exit codes: 0 pass, 1 any FAIL, 2 refused. Secrets are never printed.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LOOPBACK = {"localhost", "127.0.0.1", "::1"}
DEFAULT_LLM_SECRETS = "D:/Code repo/Intelligence corner/ic-phases/p16/deploy/secrets.env"
SKILL = "data-modeling"
FULL_PROMPT = ("Model my business data: propose table roles and grain, relationships from the "
               "foreign keys, and three core measures for my approval.")
DELTA_MEASURE_PROMPT = ("Propose a governed measure total_credit_limit: the sum of "
                        "customers.credit_limit, which has no time column, for my approval.")
MODEL_TABLES = ("model_tables", "model_relationships", "model_measures", "model_indexes")


# ------------------------------------------------------------------ scripted definitions

def _table(name, role, grain, pk, atd=None):
    d = {"name": name, "relation": f"public.{name}", "role": role, "grain": grain,
         "primary_key": pk}
    if atd:
        d["agg_time_dimension"] = atd
    return d


TABLES = [
    _table("regions", "dimension", "one row per sales region", ["id"]),
    _table("sales_reps", "dimension", "one row per sales representative", ["id"]),
    _table("customers", "dimension", "one row per customer", ["id"]),
    _table("products", "dimension", "one row per product (SKU)", ["id"]),
    _table("sales_orders", "fact", "one row per sales order", ["id"],
           "public.sales_orders.order_date"),
    _table("order_lines", "fact", "one row per order line", ["id"],
           "public.sales_orders.order_date"),
    _table("invoices", "fact", "one row per invoice", ["id"], "public.invoices.invoice_date"),
    _table("payments", "fact", "one row per payment", ["id"], "public.payments.paid_at"),
    _table("inventory_snapshots", "fact", "one row per product per weekly snapshot",
           ["product_id", "snapshot_date"], "public.inventory_snapshots.snapshot_date"),
]

SIMPLE_MEASURES = [
    {"name": "order_count", "type": "simple", "base_relation": "public.sales_orders",
     "agg": "count", "expr": "id", "agg_time_dimension": "public.sales_orders.order_date",
     "filter": [{"dimension": "public.sales_orders.status", "op": "not_in",
                 "values": ["cancelled"]}],
     "label": "Orders", "description": "Sales orders that were not cancelled"},
    {"name": "net_sales", "type": "simple", "base_relation": "public.order_lines",
     "agg": "sum", "expr": "qty * unit_price * (1 - discount_pct / 100)",
     "agg_time_dimension": "public.sales_orders.order_date",
     "filter": [{"dimension": "public.sales_orders.status", "op": "not_in",
                 "values": ["cancelled", "returned"]}],
     "label": "Net sales", "description": "Line revenue after discount, excluding cancelled and returned orders"},
    {"name": "invoiced_amount", "type": "simple", "base_relation": "public.invoices",
     "agg": "sum", "expr": "amount", "agg_time_dimension": "public.invoices.invoice_date",
     "label": "Invoiced", "description": "Invoice amounts by invoice date"},
    {"name": "collected_amount", "type": "simple", "base_relation": "public.payments",
     "agg": "sum", "expr": "amount", "agg_time_dimension": "public.payments.paid_at",
     "label": "Collected", "description": "Payments received by payment date"},
    {"name": "stock_on_hand", "type": "simple", "base_relation": "public.inventory_snapshots",
     "agg": "sum", "expr": "on_hand",
     "agg_time_dimension": "public.inventory_snapshots.snapshot_date",
     "non_additive_dimension": {"name": "public.inventory_snapshots.snapshot_date",
                                "window_choice": "max", "window_groupings": []},
     "additivity": "semi_additive", "label": "Stock on hand",
     "description": "Units on hand at the last snapshot of the period"},
]

COMPOSED_MEASURES = [
    {"name": "average_order_value", "type": "ratio",
     "numerator": {"name": "net_sales"}, "denominator": {"name": "order_count"},
     "label": "Average order value", "description": "Net sales divided by orders"},
    {"name": "net_sales_mom_growth", "type": "derived",
     "expr": "(current - previous) / NULLIF(previous, 0)",
     "input_metrics": [{"name": "net_sales", "alias": "current"},
                       {"name": "net_sales", "alias": "previous", "offset_window": "1 month"}],
     "label": "Net sales growth", "description": "Month-over-month change in net sales"},
]

VIEW = {"name": "weekly_stock",
        "sql": ("SELECT p.category AS category, s.snapshot_date AS snapshot_date, "
                "SUM(s.on_hand) AS on_hand FROM public.inventory_snapshots AS s "
                "JOIN public.products AS p ON p.id = s.product_id "
                "GROUP BY p.category, s.snapshot_date"),
        "description": "Stock on hand per category and weekly snapshot"}

MATVIEW = {"name": "category_month_sales_mv",
           "sql": ("SELECT p.category AS category, DATE_TRUNC('month', o.order_date) AS month, "
                   "SUM(l.qty * l.unit_price * (1 - l.discount_pct / 100)) AS gross "
                   "FROM public.order_lines AS l "
                   "JOIN public.sales_orders AS o ON o.id = l.order_id "
                   "JOIN public.products AS p ON p.id = l.product_id "
                   "GROUP BY p.category, DATE_TRUNC('month', o.order_date)"),
           "description": "Gross sales per category and month (snapshot as of the last refresh)"}

INDEX = {"name": "ix_category_month_sales_mv_month",
         "relation": "model.category_month_sales_mv", "columns": ["month"]}

# Dependencies decide the order: a time column or filter on another table needs its
# relationship first, ratios need their inputs, an index or refresh needs the matview.
APPROVAL_ORDER = ["relationships", "tables", "simple_measures", "composed_measures",
                  "view", "materialized_view", "index", "refresh"]

_OBJECT = {"tables": "table", "simple_measures": "measure", "composed_measures": "measure",
           "view": "view", "materialized_view": "materialized_view", "index": "index"}
_STAGE_DEFS = {"tables": TABLES, "simple_measures": SIMPLE_MEASURES,
               "composed_measures": COMPOSED_MEASURES, "view": [VIEW],
               "materialized_view": [MATVIEW], "index": [INDEX]}


def stage_items(stage: str) -> list:
    """propose_model_change items for one approval stage."""
    if stage == "refresh":
        return [{"object": "materialized_view", "op": "refresh",
                 "definition": {"name": MATVIEW["name"]}}]
    return [{"object": _OBJECT[stage], "op": "create", "definition": dict(d)}
            for d in _STAGE_DEFS[stage]]


# ------------------------------------------------------------------ pure helpers

def stack_config(stack: str) -> dict:
    m = re.fullmatch(r"p(\d+)", stack or "")
    if not m:
        raise ValueError(f"STACK must be 'pN', got {stack!r}")
    base = 9000 + 10 * int(m.group(1))
    return {"ui": f"http://127.0.0.1:{base}", "base": f"http://127.0.0.1:{base + 1}",
            "dash": f"http://127.0.0.1:{base + 2}", "app": f"ic-{stack}-app-1",
            "pg": f"ic-{stack}-postgres-1", "wh": f"ic-{stack}-warehouse"}


def _secret_line(path: str, key: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip("\r\n \t\"'")
    except OSError:
        pass
    return ""


def _argval(argv, flag):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return ""


def load_config(env, argv) -> dict:
    cfg = stack_config(env.get("STACK", "p26"))
    argv = list(argv)
    ops = (env.get("OPS_KEY") or "").strip("\r\n \t\"'")
    if not ops:
        ops = _secret_line(_argval(argv, "--secrets"), "OPS_KEY")
    cfg["ops_key"] = ops
    cfg["llm_secrets"] = _argval(argv, "--llm-secrets") or DEFAULT_LLM_SECRETS
    cfg["tools_only"] = "--tools-only" in argv
    cfg["agent_only"] = "--agent-only" in argv
    cfg["timeout"] = int(env.get("SCENARIO_TIMEOUT") or 900)
    return cfg


def refusal(cfg, env):
    if env.get("SCENARIO_DEMO_STACK") != "1":
        return "set SCENARIO_DEMO_STACK=1 only on a demo stack"
    for k in ("base", "ui"):
        if (urllib.parse.urlparse(cfg[k]).hostname or "") not in LOOPBACK:
            return f"{k} URL is not loopback"
    if not cfg.get("ops_key"):
        return "no ops key (set OPS_KEY or pass --secrets <path>)"
    return None


def llm_keys_present(path: str) -> bool:
    """True when an Anthropic or OpenAI key line has a real value (presence only)."""
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        v = _secret_line(path, key)
        if v and "REPLACE_ME" not in v:
            return True
    return False


def agent_check_mode(active_counts: dict) -> str:
    """'full' when the stack has no active model rows, else 'delta'."""
    return "full" if all(int(active_counts.get(k, 0) or 0) == 0 for k in MODEL_TABLES) else "delta"


def parse_check_lines(text: str) -> dict:
    out = {"PASS": [], "SKIP": [], "FAIL": []}
    for line in (text or "").splitlines():
        m = re.match(r"^(PASS|SKIP|FAIL) (\d+)\b", line)
        if m:
            out[m.group(1)].append(int(m.group(2)))
    return out


# ------------------------------------------------------------------ flow

class Fail(Exception):
    pass


class Skip(Exception):
    pass


class Scenario:
    def __init__(self, cfg):
        self.c = cfg
        self.key = cfg["ops_key"]
        self.refusals = []

    # transport
    def raw(self, method, url, body=None, headers=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        h = dict(headers or {})
        if body is not None:
            h["content-type"] = "application/json"
        for attempt in (1, 2):
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 1:
                    time.sleep(2)
                    continue
                return e.code, e.read()
            except (urllib.error.URLError, OSError) as e:
                raise Fail(f"{method} {url.split('?')[0]} unreachable: {type(e).__name__}")
        raise Fail("unreachable")

    def call(self, method, path, body=None, ok=(200,), timeout=60):
        st, b = self.raw(method, self.c["base"] + path, body, {"x-ops-key": self.key}, timeout)
        if st not in ok:
            raise Fail(f"{method} {path} -> HTTP {st}: {b[:200].decode('utf-8', 'replace')}")
        try:
            return st, (json.loads(b) if b else {})
        except ValueError:
            return st, {}

    def get(self, path):
        return self.call("GET", path)[1]

    def sh(self, args, stdin=None):
        env = dict(os.environ, MSYS_NO_PATHCONV="1")
        return subprocess.run(args, input=stdin, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env)

    def psql(self, sql):
        r = self.sh(["docker", "exec", "-i", self.c["pg"], "psql", "-U", "agent", "-d",
                     "agent_memory", "-v", "ON_ERROR_STOP=1", "-At"], sql)
        if r.returncode != 0:
            raise Fail("psql: " + r.stderr[:300])
        return r.stdout.strip()

    def whsql(self, sql):
        r = self.sh(["docker", "exec", "-i", self.c["wh"], "psql", "-U", "wh_admin", "-d",
                     "warehouse", "-v", "ON_ERROR_STOP=1", "-At"], sql)
        if r.returncode != 0:
            raise Fail("warehouse psql: " + r.stderr[:300])
        return r.stdout.strip()

    def counts(self):
        parts = " || ',' || ".join(f"(SELECT count(*) FROM {t} WHERE status = 'active')::text"
                                   for t in MODEL_TABLES)
        vals = [int(x) for x in self.psql(f"SELECT {parts}").split(",")]
        return dict(zip(MODEL_TABLES, vals))

    def digests(self):
        parts = " || ',' || ".join(
            f"COALESCE((SELECT md5(string_agg(t::text, '|' ORDER BY t.id)) FROM {t} t), '')"
            for t in MODEL_TABLES)
        return self.psql(f"SELECT {parts}")

    # app container programs
    def in_app(self, call_src: str, data: dict):
        prog = (
            "import asyncio, json\n"
            f"P = json.loads({json.dumps(json.dumps(data))})\n"
            "async def main():\n"
            "    from app.agents.graph import ctx_phone\n"
            "    from app import modeltools as mt, guardrails, db\n"
            "    ctx_phone.set('ui:scnmodel')\n"
            "    try:\n"
            f"        out = {call_src}\n"
            "        print('RESULT ' + json.dumps(out, default=str))\n"
            "    finally:\n"
            "        await db.close_pool()\n"
            "asyncio.run(main())\n")
        r = self.sh(["docker", "exec", "-i", self.c["app"], "python", "-"], prog)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), "")
        if not line:
            raise Fail("app program failed: " + (r.stderr or r.stdout)[-300:])
        return json.loads(line[len("RESULT "):])

    def tool(self, name, args):
        return self.in_app("await getattr(mt, P['tool']).ainvoke(P['args'])",
                           {"tool": name, "args": args})

    def sql(self, text):
        return self.in_app("await guardrails.run_readonly_sql(P['sql'])", {"sql": text})

    # approvals
    def proposed(self, run_id=None):
        rows = self.get("/api/actions?status=proposed&kind=model_change&limit=200")
        out = []
        for a in rows:
            p = a.get("payload")
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except ValueError:
                    p = {}
            if run_id is None or a.get("task_run_id") == run_id:
                out.append((a, p or {}))
        return out

    def approve(self, action_id):
        _, res = self.call("POST", f"/api/actions/{action_id}/approve", ok=(200,))
        if res.get("status") != "executed":
            raise Fail(f"action {action_id} ended {res.get('status')}")

    def propose_and_approve(self, stage):
        """Propose a stage's items, approve each new card. 'pending' or approved already is fine."""
        items = stage_items(stage)
        done = 0
        self.refusals = []
        for i in range(0, len(items), 12):
            res = self.tool("propose_model_change", {"items": items[i:i + 12]})
            if isinstance(res, str):
                self.refusals += [ln[:160] for ln in res.splitlines() if "refused:" in ln]
        wanted = {d["name"] for d in _STAGE_DEFS.get(stage, [{"name": MATVIEW["name"]}])}
        for a, p in self.proposed():
            if p.get("name") in wanted and p.get("object") == (
                    "materialized_view" if stage == "refresh" else _OBJECT[stage]):
                self.approve(a["id"])
                done += 1
        return done

    def wait_run(self, run_id):
        end = time.time() + self.c["timeout"]
        while time.time() < end:
            time.sleep(4)
            for r in self.get("/api/runs?limit=50"):
                if r["id"] == run_id and r["status"] in ("done", "failed"):
                    return r
        raise Fail(f"run {run_id} did not finish in time")

    def ask(self, operator, text):
        _, b = self.call("POST", "/api/ask", {"message": text, "operator": operator})
        return b["run_id"]

    def health_wait(self, seconds=120):
        end = time.time() + seconds
        while time.time() < end:
            try:
                st, _ = self.raw("GET", self.c["base"] + "/health", timeout=5)
                if st == 200:
                    return
            except Fail:
                pass
            time.sleep(3)
        raise Fail("app did not become healthy")

    # ---------------------------------------------------------------- checks
    def c1_preflight(self):
        st, _ = self.raw("GET", self.c["base"] + "/health")
        if st != 200:
            raise Fail(f"/health -> {st}")
        self.get("/api/model/tables")
        return "preflight: /health and /api/model/tables answer"

    def c2_agent(self):
        if self.c["tools_only"]:
            raise Skip("--tools-only")
        if not llm_keys_present(self.c["llm_secrets"]):
            raise Skip("no LLM key")
        before = self.counts()
        mode = agent_check_mode(before)
        self.call("PATCH", f"/api/skills/{SKILL}", {"enabled": True})
        try:
            op, text = ("scnmodel", FULL_PROMPT) if mode == "full" else ("scnmodeldelta", DELTA_MEASURE_PROMPT)
            run_id = self.ask(op, text)
            r = self.wait_run(run_id)
            if r["status"] != "done":
                raise Fail(f"agent run ended {r['status']}")
            cards = self.proposed(run_id)
            if not cards:
                raise Fail("the run proposed no model_change card")
            measures = [p for _, p in cards if p.get("object") == "measure"]
            for p in measures:
                ev = p.get("evidence") or {}
                samples = ev.get("samples") or []
                if not ev.get("sql") or not (1 <= len(samples) <= 31 or "sample_value" in ev):
                    raise Fail(f"measure card {p.get('name')} lacks evidence")
            if mode == "full":
                objs = {p.get("object") for _, p in cards}
                if not {"table", "relationship", "measure"} <= objs:
                    raise Fail(f"full mode wants table, relationship and measure cards, got {sorted(objs)}")
                for _, p in cards:
                    if p.get("object") == "relationship":
                        mr = (p.get("evidence") or {}).get("match_rate")
                        if p.get("origin") == "foreign_key" and not isinstance(mr, (int, float)):
                            raise Fail("relationship card has no numeric match_rate")
                if not any(p.get("object") == "relationship" and p.get("origin") == "foreign_key"
                           for _, p in cards):
                    raise Fail("no foreign_key relationship card")
            else:
                if not any((p.get("evidence") or {}).get("sql") and "sample_value" in (p.get("evidence") or {})
                           for p in measures):
                    raise Fail("delta mode wants a measure card with sql and a sample_value")
            for a, _ in cards:
                self.call("POST", f"/api/actions/{a['id']}/reject")
            after = self.counts()
            if after != before:
                raise Fail("model rows changed after rejecting the agent's cards")
            return ("agent proposals (full mode)" if mode == "full"
                    else "agent proposals (delta mode: model already approved)")
        finally:
            try:
                self.call("PATCH", f"/api/skills/{SKILL}", {"enabled": False})
            except Fail:
                pass

    def c3_relationships(self):
        self.tool("propose_relationships_from_keys", {})
        ids = [a["id"] for a, p in self.proposed() if p.get("object") == "relationship"]
        if ids:
            _, res = self.call("POST", "/api/actions/bulk-approve", {"ids": ids})
            if res.get("failed"):
                raise Fail(f"{res['failed']} relationship approvals failed")
        n = int(self.psql("SELECT count(*) FROM model_relationships WHERE status = 'active' "
                          "AND origin = 'foreign_key'"))
        if n != 10:
            raise Fail(f"expected 10 foreign_key relationships, found {n}")
        return "10 foreign-key relationships approved" if ids else "10 foreign-key relationships (already approved)"

    def _stage_check(self, stages, table, names):
        for s in stages:
            self.propose_and_approve(s)
        have = set(self.psql(f"SELECT name FROM {table} WHERE status = 'active'").splitlines())
        missing = sorted(set(names) - have)
        if missing:
            raise Fail(f"not active: {', '.join(missing)} {'; '.join(self.refusals[:2])}")

    def c4_tables_simple(self):
        self._stage_check(["tables"], "model_tables", [t["name"] for t in TABLES])
        self._stage_check(["simple_measures"], "model_measures", [m["name"] for m in SIMPLE_MEASURES])
        return "table roles and 5 simple measures approved"

    def c5_composed(self):
        self._stage_check(["composed_measures"], "model_measures", [m["name"] for m in COMPOSED_MEASURES])
        return "ratio and period-over-period measures approved"

    def c6_once(self):
        row = self.psql("SELECT id FROM actions WHERE kind = 'model_change' AND status = 'executed' "
                        "ORDER BY id DESC LIMIT 1")
        if not row:
            raise Fail("no executed model_change action to retry")
        before = self.counts()
        st, _ = self.call("POST", f"/api/actions/{row}/approve", ok=(409, 200))
        if st != 409:
            raise Fail(f"second approve answered {st}, wanted 409")
        if self.counts() != before:
            raise Fail("row counts changed on a second approve")
        return "an executed action cannot be approved twice"

    def c7_reject(self):
        probe = {"name": "scenario_reject_probe", "type": "simple", "base_relation": "public.invoices",
                 "agg": "count", "expr": "id", "agg_time_dimension": "public.invoices.invoice_date"}
        self.tool("propose_model_change", {"items": [{"object": "measure", "op": "create",
                                                      "definition": probe}]})
        hit = [a for a, p in self.proposed() if p.get("name") == probe["name"]]
        if not hit:
            raise Fail("probe measure was not proposed")
        self.call("POST", f"/api/actions/{hit[0]['id']}/reject")
        if self.psql("SELECT count(*) FROM model_measures WHERE name = 'scenario_reject_probe'") != "0":
            raise Fail("a rejected measure left a row")
        return "a rejected proposal writes nothing"

    def c8_warehouse(self):
        for s in ("view", "materialized_view", "index", "refresh"):
            self.propose_and_approve(s)
        opts = self.whsql("SELECT array_to_string(reloptions, ',') FROM pg_class "
                          "WHERE oid = 'model.weekly_stock'::regclass")
        if "security_invoker=true" not in opts:
            raise Fail(f"model.weekly_stock reloptions: {opts or 'none'}")
        kind = self.whsql("SELECT relkind FROM pg_class WHERE oid = 'model.category_month_sales_mv'::regclass")
        if kind != "m":
            raise Fail(f"category_month_sales_mv relkind {kind!r}, wanted m")
        idx = self.whsql("SELECT count(*) FROM pg_indexes WHERE schemaname = 'model' "
                         f"AND indexname = '{INDEX['name']}'")
        if idx != "1":
            raise Fail("matview index is missing")
        rows = self.sql("SELECT count(*) AS n FROM model.weekly_stock")
        if not rows or int(rows[0]["n"]) < 1:
            raise Fail("model.weekly_stock returned no rows through guardrails")
        return "view (security_invoker), matview, index and refresh in the warehouse"

    def c9_query(self):
        out = self.tool("query_measure", {
            "measure": "net_sales", "by": ["regions.name"], "grain": "month",
            "range": {"start": "2026-07-01", "end": "2026-10-01"}})
        try:
            doc = json.loads(out)
        except (TypeError, ValueError):
            raise Fail(f"net_sales did not return JSON: {str(out)[:200]}")
        if len(doc.get("rows") or []) < 5:
            raise Fail(f"only {len(doc.get('rows') or [])} rows")
        if not str(doc.get("sql", "")).lstrip().upper().startswith(("SELECT", "WITH")):
            raise Fail("compiled SQL is not a SELECT")
        gm = self.tool("query_measure", {"measure": "gross_margin"})
        if not str(gm).startswith("NOT MODELED:"):
            raise Fail(f"gross_margin answered {str(gm)[:120]}")
        return "query_measure answers from compiled SQL and says NOT MODELED for the rest"

    def c10_api(self):
        data = self.get("/api/model/measures")["measures"]
        by = {m["name"]: m for m in data}
        want = [m["name"] for m in SIMPLE_MEASURES + COMPOSED_MEASURES]
        missing = [n for n in want if n not in by]
        if missing:
            raise Fail(f"measures missing from the API: {', '.join(missing)}")
        for n in want:
            m = by[n]
            if not m.get("compiled_sql") or m.get("compile_error"):
                raise Fail(f"{n} did not compile: {str(m.get('compile_error'))[:160]}")
        st, _ = self.raw("GET", self.c["ui"] + "/context/model")
        if st != 200:
            raise Fail(f"/context/model -> {st}")
        st, _ = self.raw("GET", self.c["ui"] + "/api/model/tables", headers={"x-ops-key": self.key})
        if st != 200:
            raise Fail(f"UI proxy /api/model/tables -> {st}")
        return "Model API compiles all 7 measures; the page and the UI proxy answer"

    def c11_reboot(self):
        before = self.digests()
        r = self.sh(["docker", "restart", self.c["app"]])
        if r.returncode != 0:
            raise Fail("docker restart failed")
        self.health_wait()
        if self.digests() != before:
            raise Fail("model rows changed across a restart")
        return "model rows are identical after a restart"

    def c12_backup_probe(self):
        wh, tmp, scratch = self.c["wh"], "/tmp/p16_model_probe.dump", "p16_restore_probe"
        try:
            d = self.sh(["docker", "exec", wh, "pg_dump", "-U", "wh_admin", "-d", "warehouse",
                         "-Fc", "-n", "ic_meta", "-n", "model", "-f", tmp])
            if d.returncode != 0:
                raise Fail("pg_dump failed: " + d.stderr[:200])
            self.sh(["docker", "exec", wh, "dropdb", "-U", "wh_admin", "--if-exists", scratch])
            c = self.sh(["docker", "exec", wh, "createdb", "-U", "wh_admin", scratch])
            if c.returncode != 0:
                raise Fail("createdb failed: " + c.stderr[:200])
            r = self.sh(["docker", "exec", wh, "pg_restore", "-U", "wh_admin", "-d", scratch,
                         "--no-owner", "--no-acl", "--exit-on-error", tmp])
            outcome = ("restore ok" if r.returncode == 0 else
                       "restore failed (expected: model views reference public views not in the dump)")
            return "backup probe: " + outcome
        finally:
            self.sh(["docker", "exec", wh, "dropdb", "-U", "wh_admin", "--if-exists", scratch])
            self.sh(["docker", "exec", wh, "rm", "-f", tmp])


CHECKS = [
    (1, "c1_preflight"), (2, "c2_agent"), (3, "c3_relationships"), (4, "c4_tables_simple"),
    (5, "c5_composed"), (6, "c6_once"), (7, "c7_reject"), (8, "c8_warehouse"),
    (9, "c9_query"), (10, "c10_api"), (11, "c11_reboot"), (12, "c12_backup_probe"),
]


def run(cfg) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    s = Scenario(cfg)
    fails = 0
    for n, name in CHECKS:
        if cfg["agent_only"] and n not in (1, 2):
            continue
        label = name.split("_", 1)[1].replace("_", " ")
        try:
            print(f"PASS {n} {getattr(s, name)()}", flush=True)
        except Skip as e:
            print(f"SKIP {n} {label}: {e}", flush=True)
        except Fail as e:
            fails += 1
            print(f"FAIL {n} {label}: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"FAIL {n} {label}: {type(e).__name__}: {str(e)[:200]}", flush=True)
    print("FAIL scenario_model" if fails else "PASS scenario_model", flush=True)
    return 1 if fails else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        cfg = load_config(os.environ, argv)
    except ValueError as e:
        print(f"REFUSED: {e}")
        return 2
    why = refusal(cfg, os.environ)
    if why:
        print(f"REFUSED: {why}")
        return 2
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
