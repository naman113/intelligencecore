"""Source and warehouse connection builders. SQLAlchemy loads lazily; passwords only via URL.create."""
import json

LOOPBACK = {"localhost", "127.0.0.1", "::1"}
CONNECT_TIMEOUT = 15
SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
MSSQL_DRIVER = "ODBC Driver 18 for SQL Server"


def rewrite_loopback(host: str) -> str:
    return "host.docker.internal" if (host or "").strip().lower() in LOOPBACK else host


def _options(env) -> dict:
    raw = env.get("IC_SRC_OPTIONS")
    if not raw:
        return {}
    try:
        o = json.loads(raw)
    except ValueError:
        return {}
    return o if isinstance(o, dict) else {}


def _port(env, default: int) -> int:
    try:
        return int(env.get("IC_SRC_PORT") or default)
    except ValueError:
        return default


def source_url(kind: str, env):
    from sqlalchemy.engine import URL

    opts = _options(env)
    host = rewrite_loopback(env.get("IC_SRC_HOST", ""))
    common = dict(host=host, database=env.get("IC_SRC_DB") or None,
                  username=env.get("IC_SRC_USER") or None, password=env.get("IC_SRC_PASSWORD") or None)
    if kind == "postgres":
        q = {"connect_timeout": str(CONNECT_TIMEOUT)}
        if opts.get("sslmode") in SSLMODES:
            q["sslmode"] = opts["sslmode"]
        return URL.create("postgresql+psycopg2", port=_port(env, 5432), query=q, **common)
    if kind == "mysql":
        return URL.create("mysql+pymysql", port=_port(env, 3306), query={"charset": "utf8mb4"}, **common)
    if kind == "mssql":
        enc = opts.get("encrypt", "yes")
        if enc not in ("yes", "no", "strict"):
            enc = "yes"
        q = {"driver": MSSQL_DRIVER, "Encrypt": enc,
             "TrustServerCertificate": "yes" if opts.get("trust_server_certificate") else "no",
             "LoginTimeout": str(CONNECT_TIMEOUT)}
        return URL.create("mssql+pyodbc", port=_port(env, 1433), query=q, **common)
    raise ValueError("unsupported source kind")


def source_engine(kind: str, env):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    args: dict = {}
    if kind == "mysql":
        args["connect_timeout"] = CONNECT_TIMEOUT
        if _options(env).get("ssl"):
            args["ssl"] = {}
    elif kind == "mssql":
        args["timeout"] = 30
    return create_engine(source_url(kind, env), poolclass=NullPool, pool_pre_ping=False, connect_args=args)


def warehouse_credentials(env) -> dict:
    try:
        port = int(env.get("IC_WH_PORT") or 5432)
    except ValueError:
        port = 5432
    return {"host": env.get("IC_WH_HOST", ""), "port": port, "database": env.get("IC_WH_DB", ""),
            "username": env.get("IC_WH_USER", ""), "password": env.get("IC_WH_PASSWORD", "")}
