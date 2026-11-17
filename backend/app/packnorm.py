"""Context-pack normalizer shared by the importer (contextpack.py) and the read-only
role kit (readonlykit.parse_pack). Pure functions, no I/O, no app imports.

Rule: nothing in a pack is dropped silently. Alias names are mapped to the canonical
ones (hide_tables / ref); anything the app does not read is returned in `ignored`;
anything it cannot parse is returned in `problems`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Section aliases seen in real packs -> canonical name.
SECTION_ALIASES = {"tables_to_hide": "hide_tables", "hidden_tables": "hide_tables"}
# Hide-entry key aliases -> canonical.
HIDE_KEY_ALIASES = ("table", "table_ref")
HIDE_ACTIONS = ("hide", "revoke_select")

TOP_KNOWN = {"pack_version", "meta", "source", "business_profile", "tables", "rules",
             "glossary", "kpis", "watch_candidates", "hide_tables", "dashboard_ideas",
             "open_questions"}
# Fields the importer reads, per list section (everything else is reported as ignored).
READ_FIELDS = {
    "source": {"propose", "confidence"},
    "tables": {"propose", "confidence"},
    "rules": {"propose", "confidence"},
    "glossary": {"propose", "confidence", "term"},
    "kpis": {"propose", "confidence", "reference_sql"},
    "watch_candidates": {"title", "condition", "cadence_minutes", "rationale", "one_shot",
                         "draft_sql", "trigger_hint", "confidence"},
    "hide_tables": {"ref", "recommended_action", "reason", "confidence"},
    "dashboard_ideas": {"title", "name", "question", "description", "audience", "layout",
                        "queries", "tables", "confidence"},
    "open_questions": {"id", "question", "why_it_matters"},
}
PROPOSE_FIELDS = {"kind", "ref", "title", "text", "columns", "tables"}
PROFILE_READ = {"summary", "facts_told", "inferences", "priorities", "recurring_work", "people"}
# Note shown next to well-known provenance fields so the owner sees why they are not stored.
PROVENANCE = {"evidence", "notes", "column_review", "category", "origin", "sql_tested", "seen_in_code"}


@dataclass
class Normalized:
    data: dict
    ignored: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def hide_ref(entry) -> str | None:
    """Canonical ref of one hide entry (a dict with ref/table, or a bare string)."""
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict):
        for k in ("ref", *HIDE_KEY_ALIASES):
            v = entry.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def normalize_pack(data) -> Normalized:
    """Return a copy of the pack with canonical section and key names, plus what was
    ignored or unparseable. Never raises on pack content."""
    if not isinstance(data, dict):
        return Normalized(data if isinstance(data, dict) else {}, [], ["pack is not a JSON object"])
    out = dict(data)
    ignored: list[str] = []
    problems: list[str] = []

    for alias, canon in SECTION_ALIASES.items():
        if alias in out:
            val = out.pop(alias)
            if canon in out and isinstance(out[canon], list) and isinstance(val, list):
                out[canon] = list(out[canon]) + list(val)
            elif canon not in out:
                out[canon] = val
            else:
                problems.append(f"both {canon} and {alias} are present and one is not a list")

    # Hide entries -> {ref, ...}
    if "hide_tables" in out:
        raw = out["hide_tables"]
        if not isinstance(raw, list):
            problems.append("hide_tables is not a list")
            out["hide_tables"] = []
        else:
            fixed = []
            for i, e in enumerate(raw):
                ref = hide_ref(e)
                if ref is None:
                    problems.append(f"hide_tables[{i}]: no ref (or table) given")
                    continue
                ent = dict(e) if isinstance(e, dict) else {}
                for k in HIDE_KEY_ALIASES:
                    ent.pop(k, None)
                ent["ref"] = ref
                act = ent.get("recommended_action")
                if act is not None and (not isinstance(act, str) or act.lower() not in HIDE_ACTIONS):
                    problems.append(f"hide_tables[{i}]: unknown recommended_action {act!r} "
                                    f"(want one of {', '.join(HIDE_ACTIONS)})")
                fixed.append(ent)
            out["hide_tables"] = fixed

    for k in out:
        if k not in TOP_KNOWN:
            ignored.append(f"section {k}")

    for sec, known in READ_FIELDS.items():
        v = out.get(sec)
        items = [v] if (sec == "source" and isinstance(v, dict)) else (v if isinstance(v, list) else [])
        counts: dict[str, int] = {}
        pcounts: dict[str, int] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            for f in it:
                if f not in known:
                    counts[f] = counts.get(f, 0) + 1
            p = it.get("propose")
            if isinstance(p, dict):
                for f in p:
                    if f not in PROPOSE_FIELDS:
                        pcounts[f] = pcounts.get(f, 0) + 1
        for f, n in sorted(counts.items()):
            ignored.append(f"{sec}.{f} x{n}")
        for f, n in sorted(pcounts.items()):
            ignored.append(f"{sec}.propose.{f} x{n}")

    bp = out.get("business_profile")
    if isinstance(bp, dict):
        for f, v in bp.items():
            if f not in PROFILE_READ:
                n = len(v) if isinstance(v, (list, dict)) else 1
                ignored.append(f"business_profile.{f} x{n}")
    return Normalized(out, ignored, problems)


def ignored_line(ignored: list[str], limit: int = 900) -> str:
    """One bounded 'ignored: ...' line for the import summary."""
    if not ignored:
        return ""
    s = "; ".join(ignored)
    if len(s) > limit:
        s = s[:limit].rsplit("; ", 1)[0] + f"; ... (+{len(ignored)} entries in total)"
    return "ignored (not stored): " + s
