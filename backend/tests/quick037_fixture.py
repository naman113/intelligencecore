"""Quick 037: a Distributor-sized semantic model (15 views, 16 relationships, 50 measures) for the
tool-result size tests, plus the pre-Quick 037 get_model rendering for the before/after count.

Row shapes follow test_model_compiler (TABLES, RELS, _m) and test_model_tools (pending payloads,
catalog snapshots). Every view is a cleaning SELECT over a src_distributor table, the way the Distributor
model was built (text dates and ids parsed with guarded CASE / NULLIF expressions).
"""
import json
from datetime import datetime, timezone

SRC = "src_distributor"

# name, role, grain, primary key, time column, columns (name, source column, kind)
_VIEWS = [
    ("jobs", "fact", "one row per job", ["job_id"], "scheduled_date",
     [("job_id", "JobID", "id"), ("customer_id", "CustomerID", "id"), ("site_id", "SiteID", "id"),
      ("technician_id", "TechnicianID", "id"), ("scheduled_date", "ScheduledDate", "date"),
      ("completed_date", "CompletedDate", "date"), ("status", "Status", "text"),
      ("job_type", "JobType", "text"), ("priority", "Priority", "text"),
      ("labour_hours", "LabourHours", "num"), ("travel_km", "TravelKm", "num"),
      ("description", "Description", "text"), ("region", "Region", "text")]),
    ("job_lines", "fact", "one row per job line", ["job_line_id"], None,
     [("job_line_id", "JobLineID", "id"), ("job_id", "JobID", "id"), ("product_id", "ProductID", "id"),
      ("qty", "Qty", "num"), ("unit_price", "UnitPrice", "num"), ("unit_cost", "UnitCost", "num"),
      ("discount_pct", "DiscountPct", "num"), ("line_type", "LineType", "text"),
      ("billable", "Billable", "text"), ("created_date", "CreatedDate", "date")]),
    ("customers", "dimension", "one row per customer", ["customer_id"], None,
     [("customer_id", "CustomerID", "id"), ("name", "Name", "text"), ("segment", "Segment", "text"),
      ("city", "City", "text"), ("postcode", "Postcode", "text"), ("credit_limit", "CreditLimit", "num"),
      ("created_date", "CreatedDate", "date"), ("is_active", "Active", "text"),
      ("account_manager", "AccountManager", "text")]),
    ("sites", "dimension", "one row per customer site", ["site_id"], None,
     [("site_id", "SiteID", "id"), ("customer_id", "CustomerID", "id"), ("site_name", "SiteName", "text"),
      ("address", "Address", "text"), ("city", "City", "text"), ("postcode", "Postcode", "text"),
      ("site_type", "SiteType", "text"), ("opened_date", "OpenedDate", "date")]),
    ("technicians", "dimension", "one row per technician", ["technician_id"], None,
     [("technician_id", "TechnicianID", "id"), ("full_name", "FullName", "text"),
      ("team", "Team", "text"), ("grade", "Grade", "text"), ("hourly_cost", "HourlyCost", "num"),
      ("hired_date", "HiredDate", "date"), ("is_active", "Active", "text")]),
    ("invoices", "fact", "one row per invoice", ["invoice_id"], "invoice_date",
     [("invoice_id", "InvoiceID", "id"), ("customer_id", "CustomerID", "id"), ("job_id", "JobID", "id"),
      ("invoice_date", "InvoiceDate", "date"), ("due_date", "DueDate", "date"),
      ("amount", "Amount", "num"), ("tax_amount", "TaxAmount", "num"), ("status", "Status", "text"),
      ("currency", "Currency", "text"), ("invoice_number", "InvoiceNumber", "text")]),
    ("invoice_lines", "fact", "one row per invoice line", ["invoice_line_id"], None,
     [("invoice_line_id", "InvoiceLineID", "id"), ("invoice_id", "InvoiceID", "id"),
      ("product_id", "ProductID", "id"), ("qty", "Qty", "num"), ("unit_price", "UnitPrice", "num"),
      ("line_total", "LineTotal", "num"), ("tax_code", "TaxCode", "text")]),
    ("payments", "fact", "one row per payment", ["payment_id"], "paid_date",
     [("payment_id", "PaymentID", "id"), ("invoice_id", "InvoiceID", "id"),
      ("paid_date", "PaidDate", "date"), ("amount", "Amount", "num"), ("method", "Method", "text"),
      ("reference", "Reference", "text")]),
    ("quotes", "fact", "one row per quote", ["quote_id"], "quote_date",
     [("quote_id", "QuoteID", "id"), ("customer_id", "CustomerID", "id"), ("quote_date", "QuoteDate", "date"),
      ("valid_until", "ValidUntil", "date"), ("status", "Status", "text"), ("amount", "Amount", "num"),
      ("won_date", "WonDate", "date"), ("lost_reason", "LostReason", "text")]),
    ("quote_lines", "fact", "one row per quote line", ["quote_line_id"], None,
     [("quote_line_id", "QuoteLineID", "id"), ("quote_id", "QuoteID", "id"),
      ("product_id", "ProductID", "id"), ("qty", "Qty", "num"), ("unit_price", "UnitPrice", "num"),
      ("discount_pct", "DiscountPct", "num")]),
    ("products", "dimension", "one row per product or service", ["product_id"], None,
     [("product_id", "ProductID", "id"), ("sku", "SKU", "text"), ("name", "Name", "text"),
      ("category", "Category", "text"), ("unit_price", "UnitPrice", "num"), ("unit_cost", "UnitCost", "num"),
      ("is_service", "IsService", "text"), ("is_discontinued", "Discontinued", "text")]),
    ("work_orders", "fact", "one row per work order", ["work_order_id"], "created_date",
     [("work_order_id", "WorkOrderID", "id"), ("asset_id", "AssetID", "id"), ("job_id", "JobID", "id"),
      ("created_date", "CreatedDate", "date"), ("closed_date", "ClosedDate", "date"),
      ("status", "Status", "text"), ("category", "Category", "text"), ("sla_hours", "SlaHours", "num")]),
    ("appointments", "fact", "one row per appointment", ["appointment_id"], "start_date",
     [("appointment_id", "AppointmentID", "id"), ("job_id", "JobID", "id"),
      ("start_date", "StartDate", "date"), ("end_date", "EndDate", "date"), ("status", "Status", "text"),
      ("arrival_window", "ArrivalWindow", "text"), ("no_show", "NoShow", "text")]),
    ("contracts", "fact", "one row per service contract", ["contract_id"], "start_date",
     [("contract_id", "ContractID", "id"), ("customer_id", "CustomerID", "id"),
      ("start_date", "StartDate", "date"), ("end_date", "EndDate", "date"),
      ("monthly_value", "MonthlyValue", "num"), ("status", "Status", "text"), ("plan", "Plan", "text")]),
    ("assets", "dimension", "one row per installed asset", ["asset_id"], None,
     [("asset_id", "AssetID", "id"), ("site_id", "SiteID", "id"), ("asset_type", "AssetType", "text"),
      ("make", "Make", "text"), ("model_name", "Model", "text"), ("installed_date", "InstalledDate", "date"),
      ("warranty_end", "WarrantyEnd", "date"), ("serial_number", "SerialNumber", "text")]),
]

# Sync and audit columns every cleaned Distributor view carries.
_AUDIT = [("created_at", "CreatedAt", "date"), ("updated_at", "UpdatedAt", "date"),
          ("synced_at", "_ic_synced_at", "date"), ("external_ref", "ExternalRef", "text"),
          ("notes", "Notes", "text")]
_VIEWS = [(n, r, g, pk, t, cols + _AUDIT) for n, r, g, pk, t, cols in _VIEWS]

_DESCRIPTIONS = {
    "jobs": "Field-service jobs from the Distributor job list, cleaned: text dates parsed, blank ids nulled. Status is one of open, scheduled, in_progress, completed, cancelled.",
    "job_lines": "Parts and labour lines booked against a job. Billable lines feed invoices; unit_cost comes from the product card at the time of booking.",
    "customers": "Customer accounts with their segment (residential, commercial, public sector) and credit limit.",
    "sites": "Customer sites where work happens; one customer can have many sites.",
    "technicians": "Field technicians and their team and grade; hourly_cost is the loaded cost per hour.",
    "invoices": "Invoices raised to customers, one per job or per contract period. Amount excludes tax; status is draft, sent, paid, overdue or void.",
    "invoice_lines": "Lines of each invoice; line_total excludes tax.",
    "payments": "Customer payments allocated to invoices; amount in the invoice currency.",
    "quotes": "Quotes sent to customers; won_date is set when a quote becomes a job.",
    "quote_lines": "Lines of each quote.",
    "products": "Parts and services that can be booked on jobs and quotes.",
    "work_orders": "Maintenance work orders raised against installed assets, with their SLA in hours.",
    "appointments": "Booked visits for jobs, with arrival windows and no-show flags.",
    "contracts": "Recurring service contracts and their monthly value.",
    "assets": "Equipment installed at customer sites (boilers, heat pumps, air handlers).",
}


def _col_sql(alias: str, src: str, out: str, kind: str) -> str:
    c = f'{alias}."{src}"'
    if kind == "id":
        return f"CASE WHEN trim({c}) ~ '^[0-9]+$' THEN trim({c})::bigint END AS {out}"
    if kind == "date":
        return (f"CASE WHEN {c} ~ '^[0-9]{{1,2}}/[0-9]{{1,2}}/[0-9]{{4}}$' THEN to_date({c}, 'MM/DD/YYYY') "
                f"WHEN {c} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' THEN to_date(left({c}, 10), 'YYYY-MM-DD') END AS {out}")
    if kind == "num":
        return (f"CASE WHEN replace(trim({c}), ',', '') ~ '^-?[0-9]+(\\.[0-9]+)?$' "
                f"THEN replace(trim({c}), ',', '')::numeric END AS {out}")
    return f"NULLIF(NULLIF(trim({c}), ''), '?') AS {out}"


def _view_sql(name: str, cols) -> str:
    a = name[0]
    body = ",\n       ".join(_col_sql(a, src, out, kind) for out, src, kind in cols)
    return (f"SELECT {body}\nFROM {SRC}.{name} AS {a}\n"
            f"WHERE {a}.\"Deleted\" IS DISTINCT FROM 'true' AND {a}.\"_ic_deleted\" IS NOT TRUE")


def _type(kind: str) -> str:
    return {"id": "bigint", "date": "date", "num": "numeric", "text": "text"}[kind]


def tables() -> list:
    out = []
    for i, (name, role, grain, pk, tcol, cols) in enumerate(_VIEWS, 1):
        out.append(dict(
            id=i, name=name, relation=f"model.{name}", object_kind="view",
            derived_sql=_view_sql(name, cols), role=role, grain=grain, primary_key=list(pk),
            agg_time_dimension=f"model.{name}.{tcol}" if tcol else None, label=None,
            description=_DESCRIPTIONS[name],
            meta={"columns": {out_: _type(kind) for out_, _, kind in cols}},
            status="active", action_id=100 + i,
            approved_at=datetime(2026, 10, 27, 9, i, tzinfo=timezone.utc),
            updated_at=datetime(2026, 10, 27, 9, i, tzinfo=timezone.utc)))
    return out


def columns() -> dict:
    cols = {f"model.{name}": {o: _type(k) for o, _, k in c} for name, *_x, c in _VIEWS}
    for name, *_x, c in _VIEWS:
        cols[f"{SRC}.{name}"] = {src: "text" for _, src, _k in c}
    return cols


_RELS = [  # from view, column, to view, column
    ("jobs", "customer_id", "customers", "customer_id"), ("jobs", "site_id", "sites", "site_id"),
    ("jobs", "technician_id", "technicians", "technician_id"), ("job_lines", "job_id", "jobs", "job_id"),
    ("job_lines", "product_id", "products", "product_id"), ("sites", "customer_id", "customers", "customer_id"),
    ("invoices", "customer_id", "customers", "customer_id"), ("invoices", "job_id", "jobs", "job_id"),
    ("invoice_lines", "invoice_id", "invoices", "invoice_id"),
    ("invoice_lines", "product_id", "products", "product_id"),
    ("payments", "invoice_id", "invoices", "invoice_id"), ("quotes", "customer_id", "customers", "customer_id"),
    ("quote_lines", "quote_id", "quotes", "quote_id"), ("quote_lines", "product_id", "products", "product_id"),
    ("work_orders", "asset_id", "assets", "asset_id"), ("appointments", "job_id", "jobs", "job_id"),
]


def relationships() -> list:
    return [dict(id=200 + i, name=f"{f}_{fc}_to_{t}", from_relation=f"model.{f}", from_columns=[fc],
                 to_relation=f"model.{t}", to_columns=[tc], cardinality="many_to_one",
                 origin="agent", match_rate=0.97 + (i % 3) / 100,
                 description=f"Each {f.rstrip('s').replace('_', ' ')} row points at one {t.rstrip('s')}.",
                 status="active", action_id=300 + i,
                 approved_at=datetime(2026, 10, 27, 10, i, tzinfo=timezone.utc),
                 updated_at=datetime(2026, 10, 27, 10, i, tzinfo=timezone.utc))
            for i, (f, fc, t, tc) in enumerate(_RELS, 1)]


def _m(i, **kw):
    base = dict(id=400 + i, label=None, description=None, base_relation=None, agg=None, expr=None,
                agg_time_dimension=None, filter=[], numerator=None, denominator=None,
                input_metrics=None, non_additive_dimension=None, additivity="additive",
                unit=None, format=None, status="active", action_id=500 + i,
                approved_at=datetime(2026, 10, 27, 11, i % 60, tzinfo=timezone.utc),
                updated_at=datetime(2026, 10, 27, 11, i % 60, tzinfo=timezone.utc))
    base.update(kw)
    return base


def _f(rel, col, op, values=None):
    d = {"dimension": f"model.{rel}.{col}", "op": op}
    if values is not None:
        d["values"] = values
    return d


def measures() -> list:
    """50 measures: 38 simple (most filtered), 6 ratios, 6 period-over-period."""
    S = []  # (name, base, agg, expr, time, filters, unit, format, description)
    j, inv, pay, q = "jobs", "invoices", "payments", "quotes"
    S += [
        ("jobs_created", j, "count", "job_id", f"model.{j}.scheduled_date", [], "jobs", "integer",
         "Every job in the job list by scheduled date, whatever its status."),
        ("jobs_completed", j, "count", "job_id", f"model.{j}.completed_date",
         [_f(j, "status", "in", ["completed"])], "jobs", "integer",
         "Jobs with status completed, by the day they were completed."),
        ("jobs_open", j, "count", "job_id", f"model.{j}.scheduled_date",
         [_f(j, "status", "in", ["open", "scheduled", "in_progress"])], "jobs", "integer",
         "Jobs not yet completed or cancelled, by scheduled date."),
        ("jobs_cancelled", j, "count", "job_id", f"model.{j}.scheduled_date",
         [_f(j, "status", "in", ["cancelled"])], "jobs", "integer", "Cancelled jobs by scheduled date."),
        ("emergency_jobs", j, "count", "job_id", f"model.{j}.scheduled_date",
         [_f(j, "priority", "in", ["P1", "emergency"]), _f(j, "status", "not_in", ["cancelled"])], "jobs",
         "integer", "Emergency (P1) jobs that were not cancelled."),
        ("labour_hours", j, "sum", "labour_hours", f"model.{j}.completed_date",
         [_f(j, "status", "in", ["completed"])], "hours", "decimal_1", "Labour hours booked on completed jobs."),
        ("travel_km", j, "sum", "travel_km", f"model.{j}.completed_date",
         [_f(j, "status", "in", ["completed"])], "km", "integer", "Kilometres travelled for completed jobs."),
        ("active_customers_with_jobs", j, "count_distinct", "customer_id", f"model.{j}.scheduled_date",
         [_f(j, "status", "not_in", ["cancelled"])], "customers", "integer",
         "Distinct customers with at least one job that was not cancelled."),
        ("technicians_dispatched", j, "count_distinct", "technician_id", f"model.{j}.scheduled_date",
         [_f(j, "status", "not_in", ["cancelled"]), _f(j, "technician_id", "is_not_null")], "technicians",
         "integer", "Distinct technicians with at least one job."),
        ("billable_revenue", "job_lines", "sum", "qty * unit_price * (1 - coalesce(discount_pct, 0) / 100)",
         f"model.{j}.completed_date", [_f("job_lines", "billable", "in", ["Y", "true"]),
                                        _f(j, "status", "in", ["completed"])], "GBP", "currency",
         "Billable value of parts and labour on completed jobs, after line discounts, before tax."),
        ("parts_cost", "job_lines", "sum", "qty * unit_cost", f"model.{j}.completed_date",
         [_f("job_lines", "line_type", "in", ["part"]), _f(j, "status", "in", ["completed"])], "GBP",
         "currency", "Cost of parts used on completed jobs."),
        ("labour_lines_value", "job_lines", "sum", "qty * unit_price", f"model.{j}.completed_date",
         [_f("job_lines", "line_type", "in", ["labour"])], "GBP", "currency", None),
        ("invoiced_amount", inv, "sum", "amount", f"model.{inv}.invoice_date",
         [_f(inv, "status", "not_in", ["draft", "void"])], "GBP", "currency",
         "Invoiced amount before tax for invoices that were sent (not draft or void), by invoice date."),
        ("invoice_count", inv, "count", "invoice_id", f"model.{inv}.invoice_date",
         [_f(inv, "status", "not_in", ["draft", "void"])], "invoices", "integer", "Invoices sent, by invoice date."),
        ("tax_invoiced", inv, "sum", "tax_amount", f"model.{inv}.invoice_date",
         [_f(inv, "status", "not_in", ["draft", "void"])], "GBP", "currency", "VAT on sent invoices."),
        ("overdue_amount", inv, "sum", "amount", f"model.{inv}.due_date",
         [_f(inv, "status", "in", ["overdue"])], "GBP", "currency",
         "Amount on invoices currently marked overdue, by due date."),
        ("overdue_invoices", inv, "count", "invoice_id", f"model.{inv}.due_date",
         [_f(inv, "status", "in", ["overdue"])], "invoices", "integer", "Invoices currently marked overdue."),
        ("invoice_lines_total", "invoice_lines", "sum", "line_total", f"model.{inv}.invoice_date",
         [_f(inv, "status", "not_in", ["draft", "void"])], "GBP", "currency", None),
        ("cash_collected", pay, "sum", "amount", f"model.{pay}.paid_date", [], "GBP", "currency",
         "Customer payments received, by the date they were paid."),
        ("payment_count", pay, "count", "payment_id", f"model.{pay}.paid_date", [], "payments", "integer", None),
        ("card_payments", pay, "sum", "amount", f"model.{pay}.paid_date",
         [_f(pay, "method", "in", ["card", "stripe"])], "GBP", "currency", "Payments taken by card."),
        ("quotes_sent", q, "count", "quote_id", f"model.{q}.quote_date",
         [_f(q, "status", "not_in", ["draft"])], "quotes", "integer", "Quotes sent to customers, by quote date."),
        ("quotes_won", q, "count", "quote_id", f"model.{q}.won_date",
         [_f(q, "status", "in", ["won", "accepted"])], "quotes", "integer", "Quotes accepted, by the day they were won."),
        ("quotes_lost", q, "count", "quote_id", f"model.{q}.quote_date",
         [_f(q, "status", "in", ["lost", "declined", "expired"])], "quotes", "integer", None),
        ("quoted_value", q, "sum", "amount", f"model.{q}.quote_date",
         [_f(q, "status", "not_in", ["draft"])], "GBP", "currency", "Value of quotes sent, before tax."),
        ("won_value", q, "sum", "amount", f"model.{q}.won_date",
         [_f(q, "status", "in", ["won", "accepted"])], "GBP", "currency", "Value of quotes won, before tax."),
        ("quote_lines_value", "quote_lines", "sum", "qty * unit_price * (1 - coalesce(discount_pct, 0) / 100)",
         f"model.{q}.quote_date", [], "GBP", "currency", None),
        ("work_orders_opened", "work_orders", "count", "work_order_id", "model.work_orders.created_date", [],
         "work orders", "integer", "Work orders raised against assets."),
        ("work_orders_closed", "work_orders", "count", "work_order_id", "model.work_orders.closed_date",
         [_f("work_orders", "status", "in", ["closed", "resolved"])], "work orders", "integer", None),
        ("sla_hours_committed", "work_orders", "sum", "sla_hours", "model.work_orders.created_date", [],
         "hours", "integer", "Total SLA hours committed on work orders opened."),
        ("appointments_booked", "appointments", "count", "appointment_id", "model.appointments.start_date",
         [_f("appointments", "status", "not_in", ["cancelled"])], "appointments", "integer", None),
        ("no_shows", "appointments", "count", "appointment_id", "model.appointments.start_date",
         [_f("appointments", "no_show", "in", ["Y", "true", "1"])], "appointments", "integer",
         "Appointments where nobody was home."),
        ("active_contracts", "contracts", "count", "contract_id", None,
         [_f("contracts", "status", "in", ["active"])], "contracts", "integer",
         "Contracts active today (no time column: one value)."),
        ("contract_mrr", "contracts", "sum", "monthly_value", None,
         [_f("contracts", "status", "in", ["active"])], "GBP", "currency",
         "Monthly recurring revenue of active contracts."),
        ("contracts_started", "contracts", "count", "contract_id", "model.contracts.start_date", [],
         "contracts", "integer", None),
        ("customer_count", "customers", "count", "customer_id", None,
         [_f("customers", "is_active", "in", ["Y", "true"])], "customers", "integer", "Active customers."),
        ("total_credit_limit", "customers", "sum", "credit_limit", None, [], "GBP", "currency", None),
        ("installed_assets", "assets", "count", "asset_id", None, [], "assets", "integer",
         "Assets installed at customer sites."),
    ]
    out = []
    for i, (name, base, agg, expr, tdim, flt, unit, fmt, desc) in enumerate(S, 1):
        out.append(_m(i, name=name, type="simple", base_relation=f"model.{base}", agg=agg, expr=expr,
                      agg_time_dimension=tdim, filter=flt, unit=unit, format=fmt, description=desc))
    ratios = [
        ("first_time_fix_rate", "jobs_completed", "jobs_created", "Completed jobs over all jobs scheduled."),
        ("average_invoice_value", "invoiced_amount", "invoice_count", "Invoiced amount per sent invoice."),
        ("quote_win_rate", "quotes_won", "quotes_sent", "Quotes won over quotes sent."),
        ("gross_margin_pct", "parts_cost", "billable_revenue", None),
        ("no_show_rate", "no_shows", "appointments_booked", "No-shows over appointments booked."),
        ("collection_rate", "cash_collected", "invoiced_amount", "Cash collected over amount invoiced."),
    ]
    n = len(out)
    for k, (name, num, den, desc) in enumerate(ratios, 1):
        out.append(_m(n + k, name=name, type="ratio", numerator={"name": num}, denominator={"name": den},
                      format="percent", description=desc))
    n = len(out)
    for k, base in enumerate(("billable_revenue", "invoiced_amount", "cash_collected", "jobs_completed",
                              "quotes_won", "contract_mrr"), 1):
        out.append(_m(n + k, name=f"{base}_mom_growth", type="derived",
                      expr="(current - previous) / NULLIF(previous, 0)",
                      input_metrics=[{"name": base, "alias": "current"},
                                     {"name": base, "alias": "previous", "offset_window": "1 month"}],
                      format="percent", description=f"Month-over-month growth of {base}."))
    assert len(out) == 50
    return sorted(out, key=lambda m: m["name"])


def pending_payloads() -> list:
    return [
        {"object": "measure", "name": "first_visit_resolution", "op": "create",
         "definition": {"name": "first_visit_resolution", "type": "simple"}},
        {"object": "measure", "name": "average_days_to_pay", "op": "create", "definition": {}},
        {"object": "relationship", "name": "work_orders_job_id_to_jobs", "op": "create",
         "definition": {"name": "work_orders_job_id_to_jobs", "from_relation": "model.work_orders",
                        "from_columns": ["job_id"], "to_relation": "model.jobs", "to_columns": ["job_id"]}},
        {"object": "table", "name": "work_orders", "op": "update", "definition": {}},
        {"object": "measure", "name": "labour_lines_value", "op": "retire", "definition": {}},
        {"object": "view", "name": "job_margin", "op": "create", "definition": {}},
    ]


def catalog_snapshot() -> dict:
    """The live catalog: the src_distributor tables under the views plus raw tables nobody modeled yet."""
    tabs = [{"schema": SRC, "table": name, "row_estimate": 1200 * (i + 1)} for i, (name, *_x) in enumerate(_VIEWS)]
    extra = ["job_notes", "job_photos", "job_status_history", "technician_shifts", "vehicle_logs",
             "stock_movements", "stock_levels", "purchase_orders", "purchase_order_lines", "suppliers",
             "timesheets", "expenses", "price_lists", "price_list_items", "customer_contacts",
             "site_contacts", "asset_readings", "asset_documents", "certificates", "inspections",
             "inspection_items", "callbacks", "complaints", "surveys", "survey_answers", "leads",
             "campaigns", "email_log", "sms_log", "users", "roles", "audit_log", "api_sync_state",
             "distributor_raw_jobs", "distributor_raw_invoices", "distributor_raw_customers", "distributor_raw_quotes",
             "distributor_raw_payments", "distributor_raw_assets", "distributor_raw_contracts", "distributor_raw_sites",
             "distributor_raw_products", "distributor_raw_appointments", "distributor_raw_work_orders", "distributor_raw_technicians",
             "distributor_raw_job_lines", "distributor_raw_invoice_lines", "distributor_raw_quote_lines", "holidays",
             "regions", "postcodes", "tax_codes", "currencies", "units"]
    tabs += [{"schema": SRC, "table": t, "row_estimate": 40 * (i + 1)} for i, t in enumerate(extra)]
    return {"source": "distributor", "tables": tabs}


def legacy_get_model(rows: dict, pending: list, candidates: list, unseeded: int, wh: dict,
                     table_freshness: dict) -> str:
    """get_model's answer before Quick 037 (f82e715 modeltools.get_model + _fit), rebuilt from
    the same rows, so the before/after count compares like with like."""
    hide = ("id", "action_id", "approved_at", "updated_at", "meta", "status")

    def clean(r):
        return {k: v for k, v in r.items() if k not in hide}

    tables = []
    for t in rows["tables"]:
        t = clean(t)
        if t.get("object_kind") in ("view", "matview"):
            t["in_warehouse"] = t["name"] in wh
        t.update(table_freshness.get(t["relation"], {}))
        tables.append(t)
    doc = {"tables": tables, "relationships": [clean(r) for r in rows["relationships"]],
           "measures": [clean(m) for m in rows["measures"]],
           "indexes": [dict(clean(i), in_warehouse=i["name"] in wh) for i in rows["indexes"]],
           "pending": pending, "candidates": candidates[:60], "unseeded_foreign_keys": unseeded}
    cap = 20000
    out = json.dumps(doc, default=str)
    if len(out) <= cap:
        return out
    doc["truncated"] = True
    cands = list(doc.get("candidates") or [])
    while cands and len(out) > cap:
        cands = cands[: len(cands) // 2]
        doc["candidates"] = cands
        out = json.dumps(doc, default=str)
    if len(out) > cap:
        for t in doc.get("tables", []):
            t.pop("description", None)
        for m in doc.get("measures", []):
            m.pop("description", None)
        out = json.dumps(doc, default=str)
    return out
