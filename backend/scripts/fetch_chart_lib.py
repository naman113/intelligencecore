"""Vendor the pinned chart library (Chart.js) into dashboards/vendor/.

DEV-TIME ONLY. This runs on the developer machine; its output is committed.
The gateway and the containers never fetch anything. Stdlib only, with zero
imports from app/.

How to upgrade: change VERSION and INTEGRITY below. That produces a NEW vendor
directory. Never edit or delete an existing directory: published dashboard
versions are immutable and keep loading the library from their own path.

    python scripts/fetch_chart_lib.py                 # download + verify + write
    python scripts/fetch_chart_lib.py --dry-run       # verify only
    python scripts/fetch_chart_lib.py --tarball X.tgz # use a local tarball
"""

import argparse
import base64
import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

LIB = "chart.js"
VERSION = "4.5.1"
TARBALL_URL = f"https://registry.npmjs.org/{LIB}/-/{LIB}-{VERSION}.tgz"
INTEGRITY = ("sha512-GIjfiT9dbmHRiYi6Nl2yFCq7kkwdkp1W/lp2J99rX0yo9tgJGn3lKQATztIjb5tVtevcBtIdICNWqlq5"
             "+E8/Pw==")
MEMBERS = {
    "package/dist/chart.umd.min.js": "chart.umd.min.js",
    "package/LICENSE.md": "LICENSE.md",
}
ENTRY = "chart.umd.min.js"
GLOBAL = "Chart"
LICENSE_ID = "MIT"

REPO_ROOT = Path(__file__).resolve().parents[2]
DASH = REPO_ROOT / "dashboards"
DEST = DASH / "vendor" / f"{LIB}-{VERSION}"
MANIFEST = DASH / "vendor.json"


def _b64(digest: bytes) -> str:
    return base64.b64encode(digest).decode()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tarball", metavar="PATH", help="use a local .tgz instead of downloading")
    ap.add_argument("--dry-run", action="store_true", help="verify and report, write nothing")
    args = ap.parse_args(argv)

    if args.tarball:
        data = Path(args.tarball).read_bytes()
    else:
        with urllib.request.urlopen(TARBALL_URL, timeout=60) as r:
            data = r.read()

    got = "sha512-" + _b64(hashlib.sha512(data).digest())
    if got != INTEGRITY:
        print(f"integrity mismatch: expected {INTEGRITY}, got {got}", file=sys.stderr)
        return 2

    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member_name, out_name in MEMBERS.items():
            member = tar.getmember(member_name)
            if not member.isfile():
                print(f"{member_name} is not a regular file", file=sys.stderr)
                return 2
            files[out_name] = tar.extractfile(member).read()

    entry = files[ENTRY]
    entry_doc = {
        "path": ENTRY,
        "bytes": len(entry),
        "sha256": hashlib.sha256(entry).hexdigest(),
        "sri": "sha384-" + _b64(hashlib.sha384(entry).digest()),
        "csp_hash": "sha256-" + _b64(hashlib.sha256(entry).digest()),
    }
    lic = files["LICENSE.md"]
    new_entry = {
        "name": LIB,
        "version": VERSION,
        "license": LICENSE_ID,
        "global": GLOBAL,
        "tarball": TARBALL_URL,
        "integrity": INTEGRITY,
        "dir": f"vendor/{LIB}-{VERSION}",
        "entry": ENTRY,
        "served_at": f"/d/_lib/{LIB}-{VERSION}/{ENTRY}",
        "files": [
            entry_doc,
            {"path": "LICENSE.md", "bytes": len(lic),
             "sha256": hashlib.sha256(lic).hexdigest()},
        ],
    }
    # Merge, never overwrite: older pinned versions stay in the manifest.
    existing = (json.loads(MANIFEST.read_text("utf-8"))
                if MANIFEST.exists() else {"libraries": []})
    libs = [x for x in existing["libraries"]
            if (x["name"], x["version"]) != (LIB, VERSION)]
    doc = {"libraries": libs + [new_entry]}

    if not args.dry_run:
        DEST.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (DEST / name).write_bytes(content)
        MANIFEST.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {DEST}; served_at {new_entry['served_at']}; sha256 {entry_doc['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
