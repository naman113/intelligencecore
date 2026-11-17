"""Live discovery diff, direct vs warehouse (Phase 14, WH-05).

Run inside the app container:  python -m scripts.wh_discovery_diff
Prints refs and counts only.
"""
import asyncio
import os
import sys


def _index(cat: dict) -> dict:
    return {(t["schema"], t["table"]): t for t in cat["tables"]}


def _col_key(c: dict):
    return (c.get("name"), c.get("type"), bool(c.get("nullable")), bool(c.get("pk")),
            repr(c.get("fk")), c.get("comment"))


async def main() -> int:
    from app import configservice, guardrails, schemadiscovery as sd, warehouse
    from app.config import get_settings
    await configservice.load_overrides()
    if not warehouse.warehouse_mode():
        print("not in warehouse mode (WAREHOUSE_URL / reader password missing)")
        return 2
    await warehouse.load_link_state()
    via = await sd.introspect()
    known = await sd.known_state()
    d_wh = sd.diff(via, known)
    saved = os.environ.pop("WAREHOUSE_URL", None)
    get_settings.cache_clear()
    await guardrails.reset_business_pool()
    try:
        direct = await sd.introspect()
        d_dir = sd.diff(direct, known)
    finally:
        if saved is not None:
            os.environ["WAREHOUSE_URL"] = saved
        get_settings.cache_clear()
        await guardrails.reset_business_pool()
    a, b = _index(direct), _index(via)
    problems = []
    for k in sorted(set(a) | set(b)):
        ref = f"{k[0]}.{k[1]}"
        if k not in a or k not in b:
            problems.append(f"{ref} only {'direct' if k in a else 'warehouse'}")
            continue
        x, y = a[k], b[k]
        if x["object_kind"] != y["object_kind"]:
            problems.append(f"{ref} object_kind {x['object_kind']} vs {y['object_kind']}")
        if x.get("comment") != y.get("comment"):
            problems.append(f"{ref} table comment differs")
        if [_col_key(c) for c in x["columns"]] != [_col_key(c) for c in y["columns"]]:
            problems.append(f"{ref} columns differ")
        if (x.get("checks") or []) != (y.get("checks") or []):
            print(f"INFO {ref} checks differ (not reachable through the FDW)")
        if x.get("row_estimate") != y.get("row_estimate"):
            print(f"INFO {ref} row_estimate {x.get('row_estimate')} vs {y.get('row_estimate')}")
    for key in ("new", "changed"):
        if d_wh["counts"][key] != d_dir["counts"][key]:
            problems.append(f"diff count {key}: direct {d_dir['counts'][key]} "
                            f"warehouse {d_wh['counts'][key]}")
    if problems:
        for p in problems:
            print(p)
        print("DISCOVERY DIFFERS")
        return 1
    print(f"DISCOVERY IDENTICAL ({len(b)} tables)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
