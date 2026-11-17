# Northwind Supply context pack: summary

1. **What this is.** A context pack for the synthetic Northwind Supply demo database (regions, reps, customers, products, orders, order lines, invoices, payments, weekly stock counts), written to the Intelligence Corner template with `deploy/demo-business.sql` as the schema source. It covers the whole schema; all meanings come from the schema file and read-only aggregate queries, so confidence is high for table and column meaning and medium for KPIs, watches and dashboard ideas.

2. **Counts.** Tables described: 9. Columns described: 48. Rules: 6 (naming, status, money, time, join, derived_column). Glossary terms: 3. KPIs: 4. Tables to hide: 0. Tables to restrict: 0. Watch candidates: 3. Dashboard ideas: 2. Open questions: 1.

3. **Check these first.**
   - q1: revenue is counted net of line discounts, because invoices are built that way. Confirm that is what you want.
   - `rule:money-units`: all amounts are assumed to be one currency in whole units.
   - Watch thresholds (900000 overdue balance, 0.8 weekly revenue ratio, any zero-stock product) are starting points.
   - No table is hidden: the demo database has no plumbing or sensitive tables, and the rehearsal needs all nine.

4. **Tables that may not exist yet.** None. All nine tables exist in the demo database that Intelligence Corner connects to.

5. **Database queried.** Yes, read-only as `demo_ro`, aggregates only, 19 queries (listed in `coverage.db_queries_run`). Every KPI, watch and dashboard query was run once; `sql_tested` is true.

6. **Self-check.** The template self-check script (`scripts/check_context_pack.py`, copied verbatim) printed `OK` with no WARN lines. The template's third file (`ic-load-batches.md`) is not produced because this pack is imported through the context-pack upload (CTX-08), so self-check item 11 does not apply. The Phase 12 fixtures (`context_pack_northwind*.json`) are separate and untouched.
