"""Quick 029: a lint-clean dashboard page and queries for tests that publish or serve one."""
from app import dashboardlint

QUERIES = [{"id": "orders", "sql": "SELECT 1 AS n", "refresh_minutes": 60}]
_SCRIPT = (
    "async function data(id) {\n"
    "  const r = await fetch('./data/' + id + location.search, { cache: 'no-store' });\n"
    "  return r.json();\n}\n"
    "data('orders').then(d => { document.title = String(d.rows.length); });\n"
)


def clean_page(body_html: str = "") -> str:
    return ("<!doctype html>\n<html lang=\"en\"><head>\n<meta charset=\"utf-8\">\n<title>T</title>\n"
            + dashboardlint.LIB_LINE + "\n\n</head><body>\n" + body_html
            + "\n<script>\n" + _SCRIPT + "</script></body></html>\n")


BAD_PAGE = clean_page("<marquee>hi</marquee>")
