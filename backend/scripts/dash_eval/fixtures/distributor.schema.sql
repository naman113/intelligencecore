-- Synthetic order-book fixture for the dashboard eval (22-13). Invented data.
CREATE TABLE customers (
    customer_id integer PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE channels (
    channel_id integer PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE orders (
    order_id integer PRIMARY KEY,
    order_date date NOT NULL,
    customer_id integer NOT NULL REFERENCES customers (customer_id),
    channel_id integer NOT NULL REFERENCES channels (channel_id),
    order_value numeric(12,2) NOT NULL
);
CREATE TABLE invoices (
    invoice_id integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers (customer_id),
    due_date date NOT NULL,
    amount numeric(12,2) NOT NULL,
    paid_amount numeric(12,2) NOT NULL,
    is_overdue boolean NOT NULL,
    age_bucket text NOT NULL
);
