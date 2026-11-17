"""Pin the in-repo chart-spec renderer (dashboards/vendor/ic-render-<version>/render.js).

DEV-TIME helper, stdlib only, zero imports from app/. It computes the byte count,
sha256, SRI (sha384) and CSP hash (sha256 base64) of render.js, appends or updates
the ic-render entry in dashboards/vendor.json (libraries[0] stays chart.js) and
rewrites the CSP_HASH_<version> line in backend/app/dashboardrender.py.

    python scripts/pin_renderer.py                      # pin
    python scripts/pin_renderer.py --check              # exit 1 when a pin differs
    python scripts/pin_renderer.py --root <app checkout> --version 1.0.0

A renderer file is never edited in place after release: a change is a new
ic-render-<version> directory (and a new constant in dashboardrender.py).
"""

import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

NAME = "ic-render"
ENTRY = "render.js"


def _b64(digest: bytes) -> str:
    return base64.b64encode(digest).decode()


def _entry(version: str, data: bytes) -> dict:
    d = f"{NAME}-{version}"
    return {
        "name": NAME,
        "version": version,
        "license": "Proprietary",
        "source": "in-repo",
        "dir": f"vendor/{d}",
        "entry": ENTRY,
        "served_at": f"/d/_lib/{d}/{ENTRY}",
        "files": [{
            "path": ENTRY,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "sri": "sha384-" + _b64(hashlib.sha384(data).digest()),
            "csp_hash": "sha256-" + _b64(hashlib.sha256(data).digest()),
        }],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]),
                    help="app checkout root (contains dashboards/ and backend/)")
    ap.add_argument("--version", default="1.0.0")
    ap.add_argument("--check", action="store_true", help="verify only, exit 1 on a difference")
    args = ap.parse_args(argv)

    root = Path(args.root)
    manifest = root / "dashboards" / "vendor.json"
    module = root / "backend" / "app" / "dashboardrender.py"
    src = root / "dashboards" / "vendor" / f"{NAME}-{args.version}" / ENTRY
    data = src.read_bytes()
    want = _entry(args.version, data)
    const = "CSP_HASH_" + args.version.replace(".", "_")
    line_re = re.compile(r'^' + const + r' = "[^"]*"$', re.M)

    doc = json.loads(manifest.read_text(encoding="utf-8"))
    libs = doc["libraries"]
    idx = next((i for i, x in enumerate(libs)
                if x.get("name") == NAME and x.get("version") == args.version), None)
    text = module.read_text(encoding="utf-8")
    m = line_re.search(text)
    new_line = f'{const} = "{want["files"][0]["csp_hash"]}"'

    if args.check:
        bad = []
        if idx is None or libs[idx] != want:
            bad.append("vendor.json entry differs from the file bytes")
        if m is None or m.group(0) != new_line:
            bad.append(f"{const} in dashboardrender.py differs from the file bytes")
        if bad:
            for b in bad:
                print(b, file=sys.stderr)
            return 1
        print("renderer pin ok")
        return 0

    if idx is None:
        libs.append(want)
    else:
        libs[idx] = want
    manifest.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    if m is None:
        print(f"{const} line not found in dashboardrender.py", file=sys.stderr)
        return 2
    module.write_text(text[:m.start()] + new_line + text[m.end():], encoding="utf-8", newline="\n")
    print(f"pinned {NAME}-{args.version}: {want['files'][0]['csp_hash']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
