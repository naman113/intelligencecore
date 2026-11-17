"""One-shot Graphiti/FalkorDB diagnostic. Run from backend/:

    uv run python scripts/graph_diag.py

Adds one episode, then inspects what actually landed in the graph and whether
search finds it (scoped and unscoped). Tells us if extraction produced edges,
whether the group filter is the problem, and which clients are in use.
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ on path

from app.config import get_settings


async def main():
    s = get_settings()
    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    from graphiti_core.nodes import EpisodeType

    kw = {"password": s.falkordb_password} if s.falkordb_password else {}
    driver = FalkorDriver(host=s.falkordb_host, port=str(s.falkordb_port), **kw)
    g = Graphiti(graph_driver=driver)
    print("llm_client:", type(g.llm_client).__name__, "| embedder:", type(g.embedder).__name__)
    await g.build_indices_and_constraints()

    gid = "diag_run"
    print("\n== adding episode ==")
    await g.add_episode(
        name="diag", episode_body="ACME is our steel vendor since June.",
        source=EpisodeType.text, source_description="diag",
        reference_time=datetime(2025, 6, 15, tzinfo=timezone.utc), group_id=gid)
    print("added.")

    # Raw graph census (FalkorDB via Cypher).
    try:
        r = await g.driver.execute_query("MATCH (n) RETURN labels(n) AS l, count(n) AS c")
        print("\n== node census ==")
        print(r)
        r2 = await g.driver.execute_query(
            "MATCH ()-[e:RELATES_TO]->() RETURN count(e) AS edges")
        print("edges(RELATES_TO):", r2)
    except Exception as ex:
        print("census query failed:", type(ex).__name__, ex)

    for label, kwargs in [("unscoped", {}), ("scoped", {"group_ids": [gid]})]:
        try:
            res = await g.search("Who is our steel vendor?", **kwargs)
            print(f"\n== search {label}: {len(res)} result(s) ==")
            for e in res[:5]:
                print("  fact=", getattr(e, "fact", None), "| group=", getattr(e, "group_id", None))
        except Exception as ex:
            print(f"search {label} failed:", type(ex).__name__, ex)

    await g.close()


if __name__ == "__main__":
    asyncio.run(main())
