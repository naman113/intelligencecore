"""Quick 006 one-off: one case per warning or critical insight.

Before Quick 006 every exploration insight was a note on one shared case
("Insights from data exploration"). This gives each warning or critical note there
its own insight case (the same note is linked to the new case as its first entry,
nothing is copied), then archives the shared case with a closing note that names the
new cases; the shared case keeps all its notes. Info insights stay where they are
(the Insights page reads them). Idempotent: a second run finds nothing to do. No
messages are sent and nothing is distilled. Never touches the business database.

Run inside the gateway container:
    docker exec agentic-app python scripts/split_insight_cases.py --dry-run
    docker exec agentic-app python scripts/split_insight_cases.py
"""

import argparse
import asyncio
import json
import sys


async def main(dry_run: bool) -> int:
    from app import findingcases
    from app.db import get_pool
    report = await findingcases.split_exploration_cases(await get_pool(), dry_run=dry_run)
    for car in report["carriers"]:
        action = "would split" if dry_run else "split"
        print(f"shared case #{car['case']}: {action} {len(car['split'])} finding(s)"
              + ("; archived" if car["archived"] else ""))
        for s in car["split"]:
            target = f"case #{s['case']}" if s["case"] else "a new case"
            print(f"  note {s['note']} ({s['severity']}) -> {target}: {s['title']}")
    if not report["carriers"]:
        print("nothing to split: no open shared exploration case")
    print(json.dumps(report, default=str))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    sys.exit(asyncio.run(main(ap.parse_args().dry_run)))
