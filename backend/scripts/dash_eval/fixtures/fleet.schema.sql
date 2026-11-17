-- Synthetic fleet fixture for the dashboard eval (22-13). Invented data.
CREATE TABLE vehicles (
    vehicle_id integer PRIMARY KEY,
    reg_no text NOT NULL
);
CREATE TABLE fuel_fills (
    fill_id integer PRIMARY KEY,
    fill_time timestamptz NOT NULL,
    vehicle_id integer NOT NULL REFERENCES vehicles (vehicle_id),
    litres numeric(10,1) NOT NULL,
    amount numeric(12,2) NOT NULL,
    odometer_km integer NOT NULL
);
CREATE TABLE trips (
    trip_id integer PRIMARY KEY,
    trip_date date NOT NULL,
    vehicle_id integer NOT NULL REFERENCES vehicles (vehicle_id),
    km integer NOT NULL
);
CREATE TABLE certificates (
    certificate_id integer PRIMARY KEY,
    vehicle_id integer NOT NULL REFERENCES vehicles (vehicle_id),
    kind text NOT NULL,
    valid_to date NOT NULL
);
CREATE TABLE budgets (
    month date PRIMARY KEY,
    fuel_budget numeric(12,2) NOT NULL
);
