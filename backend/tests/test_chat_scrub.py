"""Phase 15 gap 1 (15-18): pasted secrets are scrubbed from inbound text."""
import pytest

from app import secretbox
from app.secretbox import PASTED_SECRET_MARK as M, scrub_pasted_secrets as scrub

CORPUS = [
    ("Connect mysql://root:hunter2pw@db.example.invalid:3306/ops", "hunter2pw"),
    ("use postgresql://u:p%40ss@h:5432/db please", "p%40ss"),
    ("Server=h;Database=d;User Id=sa;Password=Secr3t;", "Secr3t"),
    ("jdbc:sqlserver://h:1433;user=sa;password=Secr3t", "Secr3t"),
    ("password: hunter2", "hunter2"),
    ("PWD=abc", "abc"),
    ("PGPASSWORD=xyzzy9", "xyzzy9"),
    ("db_password = 'a b c'", "a b c"),
    ('passwd="q w e"', "q w e"),
]
NOOP = ["show me sales by region", "mysql://reports@host/db", "the password policy is strict", ""]


def test_exact_outputs():
    assert scrub("Connect mysql://root:hunter2pw@db.example.invalid:3306/ops") == (
        f"Connect mysql://root:{M}@db.example.invalid:3306/ops", 1)
    assert scrub("Server=h;Database=d;User Id=sa;Password=Secr3t;") == (
        f"Server=h;Database=d;User Id=sa;Password={M};", 1)


def test_url_keeps_parts():
    out, n = scrub("postgresql://u:p%40ss@h:5432/db")
    assert n == 1 and out == f"postgresql://u:{M}@h:5432/db"


@pytest.mark.parametrize("text,secret", CORPUS)
def test_secret_removed_once(text, secret):
    out, n = scrub(text)
    assert secret not in out and M in out and n == 1


@pytest.mark.parametrize("text,secret", CORPUS)
def test_idempotent(text, secret):
    once = scrub(text)[0]
    assert scrub(once) == (once, 0)


@pytest.mark.parametrize("text", NOOP)
def test_noop(text):
    assert scrub(text) == (text, 0)


@pytest.mark.parametrize("val", [None, 5, {"a": 1}])
def test_non_str(val):
    assert scrub(val) == (val, 0)


@pytest.mark.parametrize("text,secret", CORPUS)
def test_looks_secret_parity(text, secret):
    from app import sourcetools
    out = scrub(text)[0]
    if sourcetools.looks_secret(text):
        assert sourcetools.looks_secret(out)



def test_prompt_explains_marker():
    from app.agents import graph
    assert secretbox.PASTED_SECRET_MARK in graph.DEFAULT_PROMPTS["supervisor"]


# ── Quick 026: Phase 15 gap 1 (raw '@', ODBC braces, empty user) and related shapes ──
# (text, secret fragments that must not survive, expected exact output)
GAP_CASES = [
    ("postgres://admin:P@ssw0rd@db.host:5432/app", ["P@ss", "ssw0rd"],
     f"postgres://admin:{M}@db.host:5432/app"),
    ("postgres://:secret9x@host/db", ["secret9x"], f"postgres://:{M}@host/db"),
    ("Driver={X};Server=h;Pwd={ab;cd};Database=d", ["ab;", "cd}", "{ab"],
     f"Driver={{X}};Server=h;Pwd={M};Database=d"),
    ("Pwd={a}}b;c};", ["a}}b", "c}"], f"Pwd={M};"),
    ("postgres://u:a/b:c@h/db", ["a/b", "b:c"], f"postgres://u:{M}@h/db"),
    ("postgres://u:a:b:c@h:5432/db", ["a:b"], f"postgres://u:{M}@h:5432/db"),
    ("mssql+pyodbc://sa:Pa%40ss%2Fw@h/db?driver=ODBC+Driver+18", ["Pa%40ss"],
     f"mssql+pyodbc://sa:{M}@h/db?driver=ODBC+Driver+18"),
    ("postgresql+psycopg://u:p@a@b@h/db", ["p@a"], f"postgresql+psycopg://u:{M}@h/db"),
    ('PASSWORD="a b"', ["a b"], f"PASSWORD={M}"),
    ("jdbc:sqlserver://h:1433;user=sa;password={Se;cr}}et};encrypt=true", ["Se;cr", "et}"],
     f"jdbc:sqlserver://h:1433;user=sa;password={M};encrypt=true"),
    ("mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BX%7D%3BUID%3Dsa%3BPWD%3Dsupers3cret%3BDATABASE%3Dd",
     ["supers3cret"], None),
    ("a postgres://u:one@h1/d and mysql://v:two@h2/d here", ["one", "two"],
     f"a postgres://u:{M}@h1/d and mysql://v:{M}@h2/d here"),
]
NOOP2 = [
    "mail naman@example.com about it",
    "ping @naman and @team at 5",
    "see https://example.com/docs and http://host:8080/a/b",
    "https://example.com:8080/path?email=a@b.com",
    "https://host:8080?x=a@b",
    "postgres://reports@host/db",
    "postgres://host:5432/db",
    "mysql://root@localhost:3306",
    "key: value; other=thing, a@b.c",
    "the pwd command prints the directory",
]


@pytest.mark.parametrize("text,secrets,expected", GAP_CASES)
def test_gap_cases_no_fragment_survives(text, secrets, expected):
    out, n = scrub(text)
    assert n >= 1 and M in out
    for s in secrets:
        assert s not in out, (s, out)
    if expected is not None:
        assert out == expected


@pytest.mark.parametrize("text,secrets,expected", GAP_CASES)
def test_gap_cases_idempotent(text, secrets, expected):
    once = scrub(text)[0]
    assert scrub(once) == (once, 0)


@pytest.mark.parametrize("text", NOOP2)
def test_noop_ordinary_text(text):
    assert scrub(text) == (text, 0)


HOSTILE = {
    "brace_a": lambda: "pwd={" + "a}" * 20000,
    "brace_dbl": lambda: "pwd={" + "}}" * 20000 + "x",
    "at_run": lambda: "http://u:" + "@" * 20000 + " tail",
    "colon_run": lambda: "x://:" + "a:" * 20000,
    "enc_semi": lambda: "password=" + "%3B" * 20000,
}


@pytest.mark.parametrize("name", list(HOSTILE))
def test_hostile_input_is_fast(name):
    import time
    text = HOSTILE[name]()
    t = time.time()
    out, n = scrub(text)
    assert time.time() - t < 2 and isinstance(out, str)
