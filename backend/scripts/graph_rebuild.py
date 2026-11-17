"""Quick 014: rebuild the knowledge graph from its Postgres sources, in process.

Run from backend/ (on a client container: /app/backend):

    uv run python scripts/graph_rebuild.py --dry-run     # plan only: what would be re-ingested
    uv run python scripts/graph_rebuild.py               # archive, clear, re-ingest, report

    docker exec -w /app/backend agentic-app /app/backend/.venv/bin/python scripts/graph_rebuild.py

Same code as POST /api/memory/rebuild (app/memory_rebuild.py): every episode
is first archived into Postgres (graph_episode_archive), then the graph is
cleared and re-ingested from learnings, closed cases, onboarding and the
archive, with the business entity types and today's hygiene (junk and
duplicates dropped, one copy per case, one-off "preferences" retired).
Idempotent: safe to run again, including after it stopped half-way. Prints
the before/after counts and the plan as JSON. Never writes the business DB.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ on path


async def main(args) -> int:
    from app import configservice, db, memory_rebuild, usage
    from app.memoryservice import memory_graph

    await configservice.load_overrides()
    try:
        result = await memory_rebuild.rebuild_graph(
            dry_run=args.dry_run, max_distill=args.max_distill, trigger="script")
        print(json.dumps(result, indent=2, default=str))
        return 0 if not result.get("stopped") else 1
    finally:
        try:
            await usage.flush()          # graph extraction calls into the usage ledger
        except Exception:
            pass
        await memory_graph.close()
        await db.close_pool()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true", help="plan only; change nothing")
    p.add_argument("--max-distill", type=int, default=50,
                   help="at most this many closed cases distilled again by the LLM "
                        "(cases with a kept distillation need none)")
    sys.exit(asyncio.run(main(p.parse_args())))
