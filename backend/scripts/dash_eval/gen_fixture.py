"""Synthetic fixture generator for the dashboard eval (22-13). Stdlib only.

    python gen_fixture.py --client mill|fleet|distributor --today 2026-11-04 --seed 7

Prints INSERT statements (about 450 days ending at --today), deterministic for the same
arguments. Nothing here reads a client database; every name and number is invented.
Mill events get a random time of day in IST and are written as timestamptz with the
+05:30 offset, so some fall on the previous UTC day.
"""

import argparse
import random
from datetime import date, datetime, timedelta

DAYS = 450
IST = "+05:30"
CLIENTS = ("mill", "fleet", "distributor")

# Table columns in schema order (the fixtures test checks them against the schema files).
COLUMNS = {
    "mill": {
        "suppliers": ["supplier_id", "name"],
        "customers": ["customer_id", "name"],
        "qualities": ["quality_id", "name"],
        "rolls_inward": ["roll_id", "weigh_time", "kg", "quality_id", "supplier_id"],
        "grns": ["grn_id", "grn_date", "supplier_id", "value"],
        "dispatches": ["dispatch_id", "dispatch_time", "customer_id", "rolls", "kg"],
        "stock_daily": ["day", "quality_id", "kg_on_hand"],
        "invoices": ["invoice_id", "customer_id", "invoice_date", "due_date", "amount",
                     "paid_amount", "is_overdue", "age_bucket"],
        "gst_monthly": ["month", "gst_payable"],
    },
    "fleet": {
        "vehicles": ["vehicle_id", "reg_no"],
        "fuel_fills": ["fill_id", "fill_time", "vehicle_id", "litres", "amount", "odometer_km"],
        "trips": ["trip_id", "trip_date", "vehicle_id", "km"],
        "certificates": ["certificate_id", "vehicle_id", "kind", "valid_to"],
        "budgets": ["month", "fuel_budget"],
    },
    "distributor": {
        "customers": ["customer_id", "name"],
        "channels": ["channel_id", "name"],
        "orders": ["order_id", "order_date", "customer_id", "channel_id", "order_value"],
        "invoices": ["invoice_id", "customer_id", "due_date", "amount", "paid_amount",
                     "is_overdue", "age_bucket"],
    },
}

SUPPLIERS = ["Northline Mills", "Sagar Fibres", "Bluepeak Traders", "Kaveri Textiles",
             "Orbit Yarns", "Meridian Supply"]
CUSTOMERS_W = ["Anand Garments", "Bharat Packaging", "Citadel Knits", "Deccan Weaves",
               "Eastern Fabrics", "Finch Apparel", "Gopal Exports", "Horizon Linen",
               "Indus Home", "Jaya Textiles", "Kiran Mills", "Lotus Wear"]
QUALITIES = ["A grade", "B grade", "C grade", "Reclaim", "Export"]
CUSTOMERS_T = ["Acme Retail", "Birch Stores", "Cobalt Mart", "Dune Outlets", "Ember Foods",
               "Fable Books", "Grove Market", "Harbor Goods", "Ivory Home", "Juniper Shop",
               "Kestrel Supply", "Lumen Co", "Maple Traders", "Nimbus Retail", "Opal Stores"]
CHANNELS = ["Web", "Retail", "Wholesale", "Partner"]
REGS = ["MH12AB1001", "MH12AB1002", "MH12CD2003", "MH14EF3004", "MH14EF3005",
        "KA05GH4006", "KA05GH4007", "GJ01JK5008"]
CERT_KINDS = ["insurance", "fitness", "permit", "pollution"]


def sql_lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (date, datetime)):
        return f"'{v.isoformat(sep=' ') if isinstance(v, datetime) else v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def _ist_time(rng: random.Random, day: date) -> str:
    minute = rng.randrange(0, 24 * 60)
    return f"{day.isoformat()} {minute // 60:02d}:{minute % 60:02d}:00{IST}"


def age_bucket(days: int) -> str:
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def _invoices(rng, today, customers, start, with_invoice_date):
    rows = []
    n = 0
    day = start
    while day <= today:
        for _ in range(rng.choice([0, 1, 1, 2])):
            n += 1
            cust = rng.randrange(1, customers + 1)
            amount = round(rng.uniform(5000, 90000), 2)
            due = day + timedelta(days=30)
            roll = rng.random()
            paid = amount if roll < 0.6 else (round(amount * rng.uniform(0.2, 0.8), 2) if roll < 0.75 else 0.0)
            overdue = due < today and paid < amount
            bucket = age_bucket((today - day).days)
            if with_invoice_date:
                rows.append([n, cust, day, due, amount, paid, overdue, bucket])
            else:
                rows.append([n, cust, due, amount, paid, overdue, bucket])
        day += timedelta(days=1)
    return rows


def _month_starts(start: date, today: date):
    d = date(start.year, start.month, 1)
    while d <= today:
        yield d
        d = date(d.year + (d.month // 12), d.month % 12 + 1, 1)


def build(client: str, today: date, seed: int) -> dict:
    """Return {table: rows} in insertion order for the client."""
    rng = random.Random(f"{client}:{seed}")
    start = today - timedelta(days=DAYS - 1)
    days = [start + timedelta(days=i) for i in range(DAYS)]
    t: dict = {}
    if client == "mill":
        t["suppliers"] = [[i + 1, n] for i, n in enumerate(SUPPLIERS)]
        t["customers"] = [[i + 1, n] for i, n in enumerate(CUSTOMERS_W)]
        t["qualities"] = [[i + 1, n] for i, n in enumerate(QUALITIES)]
        rolls, grns, disp, stock = [], [], [], []
        rid = gid = did = 0
        hand = {q: 4000.0 + 500 * q for q in range(1, len(QUALITIES) + 1)}
        for day in days:
            for _ in range(rng.randint(8, 20)):
                rid += 1
                rolls.append([rid, _ist_time(rng, day), round(rng.uniform(15, 40), 1),
                              rng.randint(1, len(QUALITIES)), rng.randint(1, len(SUPPLIERS))])
            for _ in range(rng.randint(1, 3)):
                gid += 1
                grns.append([gid, day, rng.randint(1, len(SUPPLIERS)),
                             round(rng.uniform(20000, 150000), 2)])
            for _ in range(rng.randint(3, 9)):
                did += 1
                r = rng.randint(2, 12)
                disp.append([did, _ist_time(rng, day), rng.randint(1, len(CUSTOMERS_W)), r,
                             round(r * rng.uniform(15, 35), 1)])
            for q in hand:
                hand[q] = max(200.0, round(hand[q] + rng.uniform(-300, 320), 1))
                stock.append([day, q, hand[q]])
        t["rolls_inward"], t["grns"], t["dispatches"], t["stock_daily"] = rolls, grns, disp, stock
        t["invoices"] = _invoices(rng, today, len(CUSTOMERS_W), start, True)
        t["gst_monthly"] = [[m, round(rng.uniform(80000, 260000), 2)]
                            for m in _month_starts(start, today)]
    elif client == "fleet":
        t["vehicles"] = [[i + 1, r] for i, r in enumerate(REGS)]
        fills, trips, certs = [], [], []
        fid = tid = cid = 0
        odo = {v: 40000 + 7000 * v for v in range(1, len(REGS) + 1)}
        for day in days:
            for v in odo:
                if rng.random() < 0.85:
                    tid += 1
                    km = rng.randint(60, 420)
                    odo[v] += km
                    trips.append([tid, day, v, km])
                    if rng.random() < 0.4:
                        fid += 1
                        litres = round(km * rng.uniform(0.2, 0.35), 1)
                        fills.append([fid, _ist_time(rng, day), v, litres,
                                      round(litres * rng.uniform(88, 96), 2), odo[v]])
        for v in range(1, len(REGS) + 1):
            for kind in CERT_KINDS:
                cid += 1
                certs.append([cid, v, kind, today + timedelta(days=rng.randint(-40, 300))])
        t["fuel_fills"], t["trips"], t["certificates"] = fills, trips, certs
        t["budgets"] = [[m, round(rng.uniform(250000, 400000), 2)]
                        for m in _month_starts(start, today)]
    elif client == "distributor":
        t["customers"] = [[i + 1, n] for i, n in enumerate(CUSTOMERS_T)]
        t["channels"] = [[i + 1, n] for i, n in enumerate(CHANNELS)]
        orders = []
        oid = 0
        for day in days:
            for _ in range(rng.randint(5, 14)):
                oid += 1
                orders.append([oid, day, rng.randint(1, len(CUSTOMERS_T)),
                               rng.randint(1, len(CHANNELS)), round(rng.uniform(800, 25000), 2)])
        t["orders"] = orders
        t["invoices"] = _invoices(rng, today, len(CUSTOMERS_T), start, False)
    else:
        raise SystemExit(f"unknown client {client!r}")
    return t


def render(client: str, today: date, seed: int, batch: int = 200) -> str:
    cols = COLUMNS[client]
    out = []
    for table, rows in build(client, today, seed).items():
        names = ", ".join(cols[table])
        for i in range(0, len(rows), batch):
            values = ",\n  ".join("(" + ", ".join(sql_lit(v) for v in r) + ")"
                                  for r in rows[i:i + batch])
            out.append(f"INSERT INTO {table} ({names}) VALUES\n  {values};")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--client", required=True, choices=CLIENTS)
    ap.add_argument("--today", required=True, help="YYYY-MM-DD, the last generated day")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    print(render(a.client, date.fromisoformat(a.today), a.seed), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
