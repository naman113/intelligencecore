"""Quick 014: the Memory page's graph view — nodes, edges and links to app
objects, read-only.

Plumbing over memoryservice's per-scope graph reads (FalkorDB keeps one graph
per group; every read here goes through memoryservice's per-group driver) and
a few Postgres lookups. No LLM, no writes, no free-form query: the owner-facing
view never runs Cypher it was handed.

  build_view(scope, q, limit, episodes, superseded) -> GET /api/memory/graph
  node_detail(group, uuid)                          -> GET /api/memory/graph/node/{uuid}
  available_scopes()                                -> the scope selector

Scopes: "everything" (every scope graph), "global", "case:<id>" (the case's
close episode in global, the entities it mentions and their neighbours), or
a graph name ("chat_<jid>", a legacy "case_<id>").

App-object links are deterministic (exact names, no fuzzy matching, the same
rule as people.py): an entity named exactly like an approved context-store
table (or Table.Column) links to the Schema page, "Case #N" (or a Case entity
with a case's exact title) and case_<id>_close episodes link to the case, and
an entity named exactly like a KPI watch links to the KPI.
"""

import logging
import re

from .db import get_pool
from .memoryservice import GLOBAL_GROUP, entity_type, memory_graph

log = logging.getLogger("graphview")

DEFAULT_LIMIT = 300
MAX_LIMIT = 600
EPISODE_LIMIT = 120
MATCH_LIMIT = 12
EVERYTHING = "everything"
_CASE_REF = re.compile(r"\bcase\s*#\s*(\d+)\b", re.I)
_CASE_EPISODE = re.compile(r"^case_(\d+)_close(?:_scoped)?$")
_NO_TABLE_LINK = {"Person", "Organization", "Site", "Case", "Machine"}


def _norm(text) -> str:
    return " ".join(str(text or "").strip().strip('"`').lower().split())


def _superseded(f: dict) -> bool:
    return f.get("invalid_at") is not None or f.get("expired_at") is not None


def entity_node(row: dict, gid: str) -> dict:
    labels = [str(x) for x in (row.get("labels") or [])]
    return {"id": str(row.get("uuid") or ""), "kind": "entity", "group": gid,
            "name": str(row.get("name") or ""), "type": entity_type(labels),
            "labels": labels, "summary": str(row.get("summary") or ""),
            "created_at": row.get("created_at"), "degree": int(row.get("degree") or 0),
            "links": []}


def episode_node(row: dict, gid: str) -> dict:
    return {"id": str(row.get("uuid") or ""), "kind": "episode", "group": gid,
            "name": str(row.get("name") or ""), "type": "Episode", "labels": ["Episodic"],
            "summary": "", "content": str(row.get("content") or ""),
            "source": str(row.get("source") or ""), "created_at": row.get("created_at"),
            "valid_at": row.get("valid_at"), "degree": 0, "links": []}


def fact_edge(row: dict, gid: str) -> dict:
    return {"id": str(row.get("uuid") or ""), "kind": "fact", "group": gid,
            "source": str(row.get("source") or ""), "target": str(row.get("target") or ""),
            "name": str(row.get("name") or ""), "fact": str(row.get("fact") or ""),
            "valid_at": row.get("valid_at"), "invalid_at": row.get("invalid_at"),
            "expired_at": row.get("expired_at"), "created_at": row.get("created_at"),
            "episodes": [str(e) for e in (row.get("episodes") or [])],
            "superseded": _superseded(row)}


def mention_edge(row: dict, gid: str) -> dict:
    ep, ent = str(row.get("episode") or ""), str(row.get("entity") or "")
    return {"id": f"m:{ep}:{ent}", "kind": "mentions", "group": gid, "source": ep,
            "target": ent, "name": "MENTIONS", "fact": "", "superseded": False}


# ── scopes ─────────────────────────────────────────────────────────────────

async def available_scopes(names: list[str] | None = None) -> list[dict]:
    """The scope selector: everything, global, each chat graph (by subject),
    each case that has a close episode in global, legacy case graphs."""
    names = names if names is not None else (await memory_graph.scope_graphs() or [])
    out = [{"id": EVERYTHING, "label": "Everything"},
           {"id": GLOBAL_GROUP, "label": "Global (known everywhere)"}]
    subjects: dict[str, str] = {}
    titles: dict[int, str] = {}
    case_ids: list[int] = []
    if GLOBAL_GROUP in names:
        for r in await memory_graph.read_episodes(GLOBAL_GROUP, limit=500,
                                                  name_prefix="case_") or []:
            m = _CASE_EPISODE.match(str(r.get("name") or ""))
            if m and int(m.group(1)) not in case_ids:
                case_ids.append(int(m.group(1)))
    try:
        pool = await get_pool()
        from .memoryservice import chat_group
        for r in await pool.fetch("SELECT jid, subject FROM chats"):
            subjects[chat_group(r["jid"])] = r["subject"] or r["jid"]
        if case_ids:
            for r in await pool.fetch("SELECT id, title FROM cases WHERE id = ANY($1::bigint[])",
                                      case_ids):
                titles[int(r["id"])] = r["title"] or ""
    except Exception:
        log.debug("scope labels unavailable", exc_info=True)
    for gid in names:
        if gid.startswith("chat_"):
            out.append({"id": gid, "label": f"Chat: {subjects.get(gid, gid)}"})
    for cid in sorted(case_ids):
        label = f"Case #{cid}" + (f" — {titles[cid]}" if titles.get(cid) else "")
        out.append({"id": f"case:{cid}", "label": label})
    for gid in names:
        if gid.startswith("case_"):
            out.append({"id": gid, "label": f"Old case graph {gid}"})
    return out


def resolve_scope(scope: str, names: list[str]) -> tuple[list[str], int | None]:
    """(scope graphs to read, case id to focus on or None). Unknown scopes
    resolve to nothing, never to a graph that does not exist."""
    scope = (scope or EVERYTHING).strip()
    if scope == EVERYTHING:
        return list(names), None
    m = re.fullmatch(r"case:(\d+)", scope)
    if m:
        cid = int(m.group(1))
        return [g for g in (GLOBAL_GROUP, f"case_{cid}") if g in names], cid
    return ([scope] if scope in names else []), None


# ── links to app objects ───────────────────────────────────────────────────

async def _link_index() -> dict:
    """Name -> app object maps, from Postgres. Empty maps when unreadable."""
    idx: dict = {"tables": {}, "columns": {}, "kpis": {}, "case_titles": {}, "cases": set()}
    try:
        pool = await get_pool()
        for r in await pool.fetch(
                """SELECT t.ref, t.schema_name, t.table_name, s.name AS source
                     FROM context_tables t JOIN context_sources s ON s.id = t.source_id
                    WHERE t.status = 'active'"""):
            link = {"kind": "table", "id": r["ref"], "label": r["table_name"],
                    "href": f"/context/schema?source={r['source']}&table={r['ref']}"}
            idx["tables"].setdefault(_norm(r["table_name"]), link)
            idx["tables"].setdefault(_norm(f"{r['schema_name']}.{r['table_name']}"), link)
        for r in await pool.fetch(
                """SELECT c.column_name, t.ref, t.schema_name, t.table_name, s.name AS source
                     FROM context_columns c JOIN context_tables t ON t.id = c.table_id
                     JOIN context_sources s ON s.id = t.source_id
                    WHERE c.status = 'active' AND t.status = 'active'"""):
            link = {"kind": "column", "id": f"{r['ref']}.{r['column_name']}",
                    "label": f"{r['table_name']}.{r['column_name']}",
                    "href": f"/context/schema?source={r['source']}&table={r['ref']}"}
            idx["columns"].setdefault(_norm(f"{r['table_name']}.{r['column_name']}"), link)
            idx["columns"].setdefault(
                _norm(f"{r['schema_name']}.{r['table_name']}.{r['column_name']}"), link)
        for r in await pool.fetch("SELECT id, title FROM watches WHERE kind = 'kpi'"):
            idx["kpis"].setdefault(_norm(r["title"]), {
                "kind": "kpi", "id": r["id"], "label": r["title"], "href": f"/kpis/{r['id']}"})
        for r in await pool.fetch("SELECT id, title FROM cases WHERE NOT sandbox"):
            idx["cases"].add(int(r["id"]))
            idx["case_titles"].setdefault(_norm(r["title"]), int(r["id"]))
    except Exception:
        log.warning("graph view: app-object links unavailable", exc_info=True)
    return idx


def links_for(node: dict, idx: dict) -> list[dict]:
    """The app objects one node links to (deterministic, exact names). Pure."""
    out: list[dict] = []
    name, kind, typ = node.get("name") or "", node.get("kind"), node.get("type")

    def case_link(cid: int) -> None:
        if cid in idx.get("cases", ()):
            out.append({"kind": "case", "id": cid, "label": f"Case #{cid}",
                        "href": f"/cases/{cid}"})

    if kind == "episode":
        m = _CASE_EPISODE.match(name)
        if m:
            case_link(int(m.group(1)))
        return out
    key = _norm(name)
    if typ not in _NO_TABLE_LINK:
        hit = idx["columns"].get(key) if "." in key else None
        hit = hit or idx["tables"].get(key)
        if hit:
            out.append(dict(hit))
    m = _CASE_REF.search(name)
    if m:
        case_link(int(m.group(1)))
    elif typ == "Case" and key in idx.get("case_titles", {}):
        case_link(idx["case_titles"][key])
    if typ != "Person" and key in idx["kpis"]:
        out.append(dict(idx["kpis"][key]))
    return out


async def attach_links(nodes: list[dict]) -> None:
    idx = await _link_index()
    for n in nodes:
        n["links"] = links_for(n, idx)


# ── the view ────────────────────────────────────────────────────────────────

async def _case_focus(cid: int) -> tuple[list[dict], set[str]]:
    """The case's close episode(s) in global and the entities they mention."""
    eps = [r for r in await memory_graph.read_episodes(
               GLOBAL_GROUP, limit=50, name_prefix=f"case_{cid}_close") or []
           if _CASE_EPISODE.match(str(r.get("name") or ""))
           and int(_CASE_EPISODE.match(str(r.get("name"))).group(1)) == cid]
    ids = [str(e["uuid"]) for e in eps]
    mentioned = {str(m["entity"]) for m in
                 await memory_graph.read_mentions(GLOBAL_GROUP, episodes=ids) or []}
    for n in await memory_graph.read_entities(GLOBAL_GROUP, limit=50) or []:
        if _CASE_REF.search(str(n.get("name") or "")) and int(
                _CASE_REF.search(str(n.get("name"))).group(1)) == cid:
            mentioned.add(str(n["uuid"]))
    return eps, mentioned


async def _neighbours(gid: str, uuids: set[str]) -> set[str]:
    out = set(uuids)
    for u in list(uuids)[:MATCH_LIMIT * 2]:
        for f in await memory_graph.read_facts(gid, touching=u, limit=200) or []:
            out.add(str(f["source"]))
            out.add(str(f["target"]))
    return out


async def build_view(scope: str = EVERYTHING, q: str = "", limit: int = DEFAULT_LIMIT,
                     episodes: bool = False, superseded: bool = True) -> dict:
    """Nodes and edges for the graph view. The first view keeps the `limit`
    most connected entities; search matches (and their neighbours) are always
    included, so a highlight never points at a node that is not drawn."""
    limit = max(10, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    names = await memory_graph.scope_graphs()
    out = {"available": names is not None, "scope": scope or EVERYTHING, "scopes": [],
           "nodes": [], "edges": [], "matches": {"nodes": [], "edges": [], "results": []},
           "truncated": False, "totals": {}}
    if names is None:
        return out
    out["scopes"] = await available_scopes(names)
    groups, case_id = resolve_scope(scope, names)

    entities: dict[str, dict] = {}
    ep_rows: dict[str, tuple[dict, str]] = {}
    forced: dict[str, set[str]] = {}          # gid -> entity uuids that must be drawn
    total_entities = 0
    out["totals"] = await memory_graph.graph_counts() or {}
    for gid in groups:
        if case_id is not None and gid == GLOBAL_GROUP:
            eps, mentioned = await _case_focus(case_id)
            for e in eps:
                ep_rows[str(e["uuid"])] = (e, gid)
            keep = await _neighbours(gid, mentioned)
            rows = await memory_graph.read_entities(gid, uuids=sorted(keep), limit=limit) or []
            total_entities += len(keep)
        else:
            rows = await memory_graph.read_entities(gid, limit=limit) or []
            total_entities += (out["totals"].get(gid) or {}).get("entities", len(rows))
        for r in rows:
            entities[str(r["uuid"])] = entity_node(r, gid)

    if q.strip():
        hits = await memory_graph.recall(q, scope=groups or None, limit=MATCH_LIMIT,
                                         include_superseded=superseded)
        out["matches"]["results"] = hits
        mnodes: set[str] = set()
        medges: set[str] = set()
        for h in hits:
            gid = h.get("group_id") or GLOBAL_GROUP
            if h["kind"] == "entity":
                mnodes.add(h["uuid"])
                forced.setdefault(gid, set()).add(h["uuid"])
            elif h["kind"] == "fact":
                medges.add(h["uuid"])
                for u in (h.get("source_uuid"), h.get("target_uuid")):
                    if u:
                        mnodes.add(u)
                        forced.setdefault(gid, set()).add(u)
            elif h["kind"] == "episode" and h.get("uuid"):
                mnodes.add(h["uuid"])
                row = (await memory_graph.read_episodes(gid, uuids=[h["uuid"]]) or [None])[0]
                if row:
                    ep_rows[h["uuid"]] = (row, gid)
                for m in await memory_graph.read_mentions(gid, episodes=[h["uuid"]]) or []:
                    forced.setdefault(gid, set()).add(str(m["entity"]))
        for gid, uuids in forced.items():
            keep = await _neighbours(gid, uuids)
            missing = sorted(u for u in keep if u not in entities)
            for r in await memory_graph.read_entities(gid, uuids=missing,
                                                      limit=len(missing) or 1) or []:
                entities[str(r["uuid"])] = entity_node(r, gid)
        out["matches"]["nodes"] = sorted(mnodes)
        out["matches"]["edges"] = sorted(medges)

    # cap: the most connected entities, plus everything a match forced in
    must = {u for s in forced.values() for u in s}
    ranked = sorted(entities.values(), key=lambda n: (n["id"] not in must, -n["degree"]))
    kept = ranked[:max(limit, len(must))]
    out["truncated"] = total_entities > len(kept)
    keep_ids = {n["id"] for n in kept}
    by_group: dict[str, list[str]] = {}
    for n in kept:
        by_group.setdefault(n["group"], []).append(n["id"])

    edges: list[dict] = []
    for gid, ids in by_group.items():
        for f in await memory_graph.read_facts(gid, among=ids) or []:
            e = fact_edge(f, gid)
            if e["superseded"] and not superseded:
                continue
            edges.append(e)

    if episodes:
        for gid in groups:
            if case_id is not None and gid == GLOBAL_GROUP:
                continue                         # the case's own episodes are already in
            for r in await memory_graph.read_episodes(gid, limit=EPISODE_LIMIT) or []:
                ep_rows.setdefault(str(r["uuid"]), (r, gid))
    ep_nodes = [episode_node(r, gid) for r, gid in ep_rows.values()]
    for gid in {g for _, g in ep_rows.values()}:
        ids = [n["id"] for n in ep_nodes if n["group"] == gid]
        for m in await memory_graph.read_mentions(gid, episodes=ids) or []:
            if str(m["entity"]) in keep_ids:
                edges.append(mention_edge(m, gid))

    nodes = kept + ep_nodes
    await attach_links(nodes)
    out["nodes"] = nodes
    out["edges"] = edges
    return out


async def node_detail(group: str, uuid: str) -> dict | None:
    """One node for the side panel: facts in and out (with text and dates,
    superseded ones flagged), the episodes that mention it, its links, and
    its neighbourhood (nodes and edges) for expanding the view. None when the
    group or node does not exist."""
    names = await memory_graph.scope_graphs()
    if names is None or group not in names or not uuid:
        return None
    rows = await memory_graph.read_entities(group, uuids=[uuid], limit=1) or []
    if rows:
        node = entity_node(rows[0], group)
        facts = [fact_edge(f, group) for f in
                 await memory_graph.read_facts(group, touching=uuid, limit=500) or []]
        other_ids = sorted({f["target"] if f["source"] == uuid else f["source"] for f in facts})
        others = {str(r["uuid"]): entity_node(r, group) for r in
                  await memory_graph.read_entities(group, uuids=other_ids,
                                                   limit=len(other_ids) or 1) or []}
        ep_ids = sorted({str(m["episode"]) for m in
                         await memory_graph.read_mentions(group, entities=[uuid]) or []})
        eps = [episode_node(r, group) for r in
               await memory_graph.read_episodes(group, uuids=ep_ids, limit=len(ep_ids) or 1) or []]
        mentions = [mention_edge({"episode": e["id"], "entity": uuid}, group) for e in eps]

        def brief(n: dict | None) -> dict | None:
            return {"id": n["id"], "name": n["name"], "type": n["type"]} if n else None

        facts_out = [{**f, "other": brief(others.get(f["target"]))}
                     for f in facts if f["source"] == uuid]
        facts_in = [{**f, "other": brief(others.get(f["source"]))}
                    for f in facts if f["target"] == uuid]
        neighbour_nodes = [node, *others.values()]
        await attach_links(neighbour_nodes + eps)
        return {"node": node, "facts_out": facts_out, "facts_in": facts_in,
                "episodes": eps, "links": node["links"],
                "neighbours": {"nodes": neighbour_nodes + eps, "edges": facts + mentions}}
    ep_rows = await memory_graph.read_episodes(group, uuids=[uuid], limit=1) or []
    if not ep_rows:
        return None
    ep = episode_node(ep_rows[0], group)
    ent_ids = sorted({str(m["entity"]) for m in
                      await memory_graph.read_mentions(group, episodes=[uuid]) or []})
    ents = [entity_node(r, group) for r in
            await memory_graph.read_entities(group, uuids=ent_ids, limit=len(ent_ids) or 1) or []]
    mentions = [mention_edge({"episode": uuid, "entity": e["id"]}, group) for e in ents]
    await attach_links([ep, *ents])
    return {"node": ep, "facts_out": [], "facts_in": [], "episodes": [ep],
            "links": ep["links"], "mentions": ents,
            "neighbours": {"nodes": [ep, *ents], "edges": mentions}}
