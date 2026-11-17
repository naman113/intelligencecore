# Source: .planning/research/context-pack-prompt.md, section "Self-check before you finish".
# Copied verbatim for Phase 13 GOLIVE-08. Do not edit the body: change the template and re-copy instead.
import json, re, sys, unicodedata
path = sys.argv[1]; raw = open(path, encoding="utf-8").read(); p = json.loads(raw)
errs, warns, seen = [], [], set()
TREF = re.compile(r"^business_db\.[^.\s]+\.[^.\s]+$")
SLUG = re.compile(r"^(rule|kpi):[a-z0-9-]{1,64}$")
QID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
BADFN = re.compile(r"\b(pg_read_file|pg_read_binary_file|pg_ls_\w+|pg_stat_file|lo_\w+|dblink\w*|pg_sleep\w*|"
                   r"pg_terminate_backend|pg_cancel_backend|\w*to_xml\w*|ts_stat|ts_rewrite|set_config|pg_reload_conf|"
                   r"pg_logical_emit_message|pg_authid|pg_shadow|pg_user_mapping|pg_largeobject)\b", re.I)
WRITE = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|call|vacuum|lock|set)\b", re.I)
SECRET = re.compile(r"(postgres(ql)?|mysql|mariadb|mongodb(\+srv)?|redis|amqp|sqlserver)://|-----BEGIN|AKIA[0-9A-Z]{16}"
                    r"|\bsk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|xox[abprs]-|password\s*[=:]", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
def hidden(s): return any(unicodedata.category(c) in ("Cf", "Co", "Cs", "Cc") and c not in "\n\t" for c in s)
def text(w, s, cap=1500):
    if not isinstance(s, str) or not s.strip(): return errs.append(f"{w}: empty text")
    if len(s) > cap: errs.append(f"{w}: {len(s)} chars > {cap}")
    if hidden(s): errs.append(f"{w}: invisible characters")
def sql(w, s):
    q = (s or "").strip().rstrip(";").strip()
    if not re.match(r"(?is)^\(?\s*(select|with)\b", q): errs.append(f"{w}: not a single SELECT")
    if ";" in q: errs.append(f"{w}: semicolon inside SQL")
    if BADFN.search(q): errs.append(f"{w}: forbidden function or catalog")
    m = WRITE.search(re.sub(r"'[^']*'", "''", q))
    if m: warns.append(f"{w}: keyword '{m.group(0)}', check it is not a write")
    if re.search(r"(?i)select\s+\*", q): warns.append(f"{w}: SELECT *")
def review(w, it):
    if it.get("confidence") not in ("high", "medium", "low"): errs.append(f"{w}: confidence missing")
    if not it.get("evidence"): errs.append(f"{w}: evidence missing")
def prop(w, pr):
    k, r = pr.get("kind"), pr.get("ref", "")
    if (k, r) in seen: errs.append(f"{w}: duplicate {k} {r}")
    seen.add((k, r))
    if k == "table":
        if not TREF.match(r): errs.append(f"{w}: bad table ref")
        if pr.get("text"): text(w, pr["text"])
        for c, t in (pr.get("columns") or {}).items():
            text(f"{w}.{c}", t)
            if isinstance(t, str) and len(t.split()) > 45: warns.append(f"{w}.{c}: over 40 words")
    elif k in ("rule", "kpi"):
        if not SLUG.match(r) or not r.startswith(k + ":"): errs.append(f"{w}: bad ref")
        text(w, pr.get("text"))
        if len(pr.get("title") or "") > 120: errs.append(f"{w}: title over 120")
        t = pr.get("tables") or []
        if len(t) > 20 or not all(TREF.match(x) for x in t): errs.append(f"{w}: bad tables list")
    elif k == "source":
        if r != "business_db": errs.append(f"{w}: source ref must be business_db")
        text(w, pr.get("text"))
    else: errs.append(f"{w}: unknown kind {k}")
if p.get("source"): prop("source", p["source"].get("propose") or {}); review("source", p["source"])
for sec in ("tables", "rules", "glossary", "kpis"):
    for i, it in enumerate(p.get(sec) or []):
        w = f"{sec}[{i}] {(it.get('propose') or {}).get('ref')}"
        prop(w, it.get("propose") or {}); review(w, it)
        if sec == "kpis": sql(w, it.get("reference_sql"))
for i, h in enumerate(p.get("hide_tables") or []):
    if not TREF.match(h.get("ref", "")): errs.append(f"hide_tables[{i}]: bad ref")
    review(f"hide_tables[{i}]", h)
for i, it in enumerate(p.get("watch_candidates") or []):
    w = f"watch_candidates[{i}]"; review(w, it); sql(w, it.get("draft_sql"))
    if not 1 <= len(it.get("title", "")) <= 120: errs.append(f"{w}: title length")
    if not 1 <= len(it.get("condition", "")) <= 2000: errs.append(f"{w}: condition length")
    if int(it.get("cadence_minutes", 0)) < 15: errs.append(f"{w}: cadence under 15 minutes")
    col = (it.get("trigger_hint") or {}).get("column", "")
    if not col or col not in (it.get("draft_sql") or ""): errs.append(f"{w}: trigger column missing from draft_sql")
for i, d in enumerate(p.get("dashboard_ideas") or []):
    review(f"dashboard_ideas[{i}]", d)
    if len(d.get("queries") or []) > 20: errs.append(f"dashboard_ideas[{i}]: over 20 queries")
    for q in d.get("queries") or []:
        w = f"dashboard_ideas[{i}].{q.get('id')}"
        if not QID.match(q.get("id", "")): errs.append(f"{w}: bad id")
        if not 5 <= int(q.get("refresh_minutes", 15)) <= 1440: errs.append(f"{w}: refresh_minutes")
        sql(w, q.get("sql"))
m = SECRET.search(raw)
if m: errs.append(f"possible secret near: {m.group(0)}")
for e in sorted(set(EMAIL.findall(raw))): warns.append(f"email-like value: {e}")
print("\n".join(["ERROR " + e for e in errs] + ["WARN  " + w for w in warns]) or "OK")
sys.exit(1 if errs else 0)
