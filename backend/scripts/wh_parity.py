"""Live parity and timing check, direct vs warehouse (Phase 14, WH-03).

Run inside the app container:  python -m scripts.wh_parity [--probe]

Every query goes through guardrails.run_readonly_sql twice: through the warehouse,
then directly against the business database (WAREHOUSE_URL removed for the run).
Prints labels, counts and timings only; never URLs, credentials or row contents.
"""
import asyncio
import os
import sys
import time

FIXED = [
    ("fixed:count", "SELECT count(*) AS n FROM invoices"),
    ("fixed:qualified", "SELECT count(*) AS n FROM public.invoices"),
    ("fixed:join_agg",
     "SELECT c.region_id, count(*) AS n, sum(i.amount) AS total FROM invoices i "
     "JOIN customers c ON c.id = i.customer_id GROUP BY c.region_id ORDER BY c.region_id"),
    ("fixed:non_shippable",
     "SELECT count(*) AS n FROM invoices WHERE invoice_date >= current_date - 30"),
]


async def _collect() -> list[tuple[str, str]]:
    from app.db import get_pool
    out = list(FIXED)
    try:
        pool = await get_pool()
        for r in await pool.fetch(
                "SELECT id, compiled_sql FROM watches WHERE compiled_sql IS NOT NULL"):
            out.append((f"watch:{r['id']}", r["compiled_sql"]))
        import json
        for r in await pool.fetch(
                "SELECT id, queries FROM dashboard_versions WHERE status = 'published'"):
            qs = r["queries"]
            qs = json.loads(qs) if isinstance(qs, str) else (qs or [])
            for q in qs:
                if isinstance(q, dict) and q.get("sql"):
                    out.append((f"dash:{r['id']}:{q.get('id')}", q["sql"]))
    except Exception as e:
        print(f"NOTE could not read watches/dashboards: {type(e).__name__}")
    return out


async def _run(sql: str):
    from app import guardrails
    t = time.perf_counter()
    try:
        rows = await guardrails.run_readonly_sql(sql)
        return rows, (time.perf_counter() - t) * 1000, None
    except Exception as e:
        return None, (time.perf_counter() - t) * 1000, type(e).__name__


def _norm(rows, ordered: bool):
    reps = [repr(sorted(r.items())) for r in rows]
    return reps if ordered else sorted(reps)


async def _probe() -> int:
    from app import configservice, guardrails, warehouse
    await configservice.load_overrides()
    await warehouse.load_link_state()
    try:
        rows = await guardrails.run_readonly_sql("SELECT count(*) AS n FROM invoices")
        print(f"PROBE OK n={rows[0]['n']}")
        return 0
    except Exception as e:
        print(f"PROBE ERROR {type(e).__name__}: {e}")
        return 1


async def main() -> int:
    from app import configservice, guardrails, warehouse
    from app.config import get_settings
    if "--probe" in sys.argv:
        return await _probe()
    await configservice.load_overrides()
    if not warehouse.warehouse_mode():
        print("not in warehouse mode (WAREHOUSE_URL / reader password missing)")
        return 2
    queries = await _collect()
    await warehouse.load_link_state()
    wh = {}
    for label, sql in queries:
        wh[label] = await _run(sql)
    saved = os.environ.pop("WAREHOUSE_URL", None)
    get_settings.cache_clear()
    await guardrails.reset_business_pool()
    direct = {}
    try:
        for label, sql in queries:
            direct[label] = await _run(sql)
    finally:
        if saved is not None:
            os.environ["WAREHOUSE_URL"] = saved
        get_settings.cache_clear()
        await guardrails.reset_business_pool()
    bad = 0
    for label, sql in queries:
        d, w = direct[label], wh[label]
        ordered = "ORDER BY" in sql.upper()
        ok = d[2] is None and w[2] is None and _norm(d[0], ordered) == _norm(w[0], ordered)
        extra = ""
        if d[2] or w[2]:
            extra = f" direct_err={d[2]} wh_err={w[2]}"
        slow = " SLOW" if w[1] > 2 * d[1] and w[1] > 1000 else ""
        n = len(w[0]) if w[0] is not None else -1
        print(f"PARITY {label} {'ok' if ok else 'MISMATCH'} rows={n} "
              f"direct_ms={d[1]:.0f} wh_ms={w[1]:.0f}{slow}{extra}")
        bad += 0 if ok else 1
    if bad:
        print(f"PARITY FAIL ({bad} mismatches)")
        return 1
    print(f"PARITY OK ({len(queries)} queries)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
