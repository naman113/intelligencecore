-- Synthetic Mill-like fixture for the dashboard eval (22-13). No client data.
CREATE TABLE suppliers (
    supplier_id integer PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE customers (
    customer_id integer PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE qualities (
    quality_id integer PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE rolls_inward (
    roll_id integer PRIMARY KEY,
    weigh_time timestamptz NOT NULL,
    kg numeric(10,1) NOT NULL,
    quality_id integer NOT NULL REFERENCES qualities (quality_id),
    supplier_id integer NOT NULL REFERENCES suppliers (supplier_id)
);
CREATE TABLE grns (
    grn_id integer PRIMARY KEY,
    grn_date date NOT NULL,
    supplier_id integer NOT NULL REFERENCES suppliers (supplier_id),
    value numeric(12,2) NOT NULL
);
CREATE TABLE dispatches (
    dispatch_id integer PRIMARY KEY,
    dispatch_time timestamptz NOT NULL,
    customer_id integer NOT NULL REFERENCES customers (customer_id),
    rolls integer NOT NULL,
    kg numeric(10,1) NOT NULL
);
CREATE TABLE stock_daily (
    day date NOT NULL,
    quality_id integer NOT NULL REFERENCES qualities (quality_id),
    kg_on_hand numeric(12,1) NOT NULL,
    PRIMARY KEY (day, quality_id)
);
CREATE TABLE invoices (
    invoice_id integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers (customer_id),
    invoice_date date NOT NULL,
    due_date date NOT NULL,
    amount numeric(12,2) NOT NULL,
    paid_amount numeric(12,2) NOT NULL,
    is_overdue boolean NOT NULL,
    age_bucket text NOT NULL
);
CREATE TABLE gst_monthly (
    month date PRIMARY KEY,
    gst_payable numeric(12,2) NOT NULL
);
