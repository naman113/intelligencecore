"""Build the pinned v2 chart library bundle (dashboards/vendor/ic-chartlib-1.0.0/).

DEV-TIME ONLY. This runs on the developer machine; its output is committed.
The gateway and the containers never fetch anything. Stdlib only, with zero
imports from app/.

The bundle is Chart.js + chartjs-plugin-annotation + chartjs-chart-matrix, each
upstream minified UMD file concatenated verbatim with one newline between. Each
tarball is verified against its npm sha512 integrity before any byte is used.

    python scripts/build_chartlib.py                       # download + verify + write
    python scripts/build_chartlib.py --dry-run             # verify only
    python scripts/build_chartlib.py --tarball-dir DIR     # offline: DIR/<name>-<version>.tgz
    python scripts/build_chartlib.py --check [--root R]    # exit 1 when the tree differs

A change is a NEW ic-chartlib-<version> directory; never edit a released one.
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

BUNDLE_NAME = "ic-chartlib"
BUNDLE_VERSION = "1.0.0"
BUNDLE_FILE = "chartlib.min.js"
GLOBAL = "Chart"

COMPONENTS = [
    {
        "name": "chart.js", "version": "4.5.1",
        "integrity": ("sha512-GIjfiT9dbmHRiYi6Nl2yFCq7kkwdkp1W/lp2J99rX0yo9tgJGn3lKQATztIjb5tVtevcBtIdICNWqlq5"
                      "+E8/Pw=="),
        "file_member": "package/dist/chart.umd.min.js", "file": "chart.umd.min.js",
        "license_member": "package/LICENSE.md", "license_out": "LICENSE-chart.js.md",
    },
    {
        "name": "chartjs-plugin-annotation", "version": "3.1.0",
        "integrity": ("sha512-EkAed6/ycXD/7n0ShrlT1T2Hm3acnbFhgkIEJLa0X+M6S16x0zwj1Fv4suv/2bwayCT3jGPdAtI9uLcAMT"
                      "oaQQ=="),
        "file_member": "package/dist/chartjs-plugin-annotation.min.js",
        "file": "chartjs-plugin-annotation.min.js",
        "license_member": "package/LICENSE.md", "license_out": "LICENSE-chartjs-plugin-annotation.md",
    },
    {
        "name": "chartjs-chart-matrix", "version": "3.1.0",
        "integrity": ("sha512-pdo8DnhFXOisjbLDgfkzZH1w0xmjGqiaAMgkQSHCEMzxQENsXTgkXImIyweDiO7SqlE5PuzjhklyBCiorQhR"
                      "mw=="),
        "file_member": "package/dist/chartjs-chart-matrix.min.js",
        "file": "chartjs-chart-matrix.min.js",
        "license_member": "package/LICENSE", "license_out": "LICENSE-chartjs-chart-matrix.txt",
    },
]


def _b64(digest: bytes) -> str:
    return base64.b64encode(digest).decode()


def _tarball_url(c: dict) -> str:
    return f"https://registry.npmjs.org/{c['name']}/-/{c['name']}-{c['version']}.tgz"


def _file_doc(path: str, data: bytes, entry: bool = False) -> dict:
    doc = {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if entry:
        doc["sri"] = "sha384-" + _b64(hashlib.sha384(data).digest())
        doc["csp_hash"] = "sha256-" + _b64(hashlib.sha256(data).digest())
    return doc


def build_entry(parts: dict[str, bytes], licenses: dict[str, bytes]) -> tuple[dict, bytes]:
    """The manifest entry and bundle bytes from the three verbatim upstream files."""
    bundle = b"\n".join(parts[c["file"]] for c in COMPONENTS)
    d = f"{BUNDLE_NAME}-{BUNDLE_VERSION}"
    comps = []
    for c in COMPONENTS:
        data = parts[c["file"]]
        comps.append({
            "name": c["name"], "version": c["version"], "tarball": _tarball_url(c),
            "integrity": c["integrity"], "file": c["file"],
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        })
    files = [_file_doc(BUNDLE_FILE, bundle, entry=True)]
    for c in COMPONENTS:
        files.append(_file_doc(c["license_out"], licenses[c["license_out"]]))
    entry = {
        "name": BUNDLE_NAME, "version": BUNDLE_VERSION, "license": "MIT", "source": "bundle",
        "global": GLOBAL, "dir": f"vendor/{d}", "entry": BUNDLE_FILE,
        "served_at": f"/d/_lib/{d}/{BUNDLE_FILE}",
        "components": comps, "files": files,
    }
    return entry, bundle


def _fetch(c: dict, tarball_dir: Path | None) -> bytes:
    if tarball_dir is not None:
        data = (tarball_dir / f"{c['name']}-{c['version']}.tgz").read_bytes()
    else:
        with urllib.request.urlopen(_tarball_url(c), timeout=60) as r:
            data = r.read()
    got = "sha512-" + _b64(hashlib.sha512(data).digest())
    if got != c["integrity"]:
        raise SystemExit(f"integrity mismatch for {c['name']}: expected {c['integrity']}, got {got}")
    return data


def _extract(c: dict, data: bytes) -> tuple[bytes, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        out = []
        for member_name in (c["file_member"], c["license_member"]):
            m = tar.getmember(member_name)
            if not m.isfile():
                raise SystemExit(f"{member_name} is not a regular file")
            out.append(tar.extractfile(m).read())
    return out[0], out[1]


def _check(root: Path) -> int:
    d = root / "dashboards"
    manifest = json.loads((d / "vendor.json").read_text("utf-8"))
    cur = next((x for x in manifest["libraries"]
                if x.get("name") == BUNDLE_NAME and x.get("version") == BUNDLE_VERSION), None)
    if cur is None:
        print("ic-chartlib entry missing from vendor.json", file=sys.stderr)
        return 1
    bdir = d / cur["dir"]
    bad = []
    try:
        data = (bdir / BUNDLE_FILE).read_bytes()
        if cur["files"][0] != _file_doc(BUNDLE_FILE, data, entry=True):
            bad.append("bundle bytes differ from the manifest")
        # The bundle is the recorded parts joined by exactly one newline.
        pos = 0
        for i, comp in enumerate(cur["components"]):
            chunk = data[pos:pos + comp["bytes"]]
            if len(chunk) != comp["bytes"] or hashlib.sha256(chunk).hexdigest() != comp["sha256"]:
                bad.append(f"component {comp['name']} does not match its recorded sha256")
            pos += comp["bytes"]
            if i < len(cur["components"]) - 1:
                if data[pos:pos + 1] != b"\n":
                    bad.append("missing newline separator")
                pos += 1
        if pos != len(data):
            bad.append("bundle has trailing bytes")
        for f in cur["files"][1:]:
            lic = (bdir / f["path"]).read_bytes()
            if _file_doc(f["path"], lic) != f:
                bad.append(f"{f['path']} differs from the manifest")
        if {p.name for p in bdir.iterdir()} != {f["path"] for f in cur["files"]}:
            bad.append("unexpected files in the bundle directory")
    except (OSError, KeyError) as e:
        bad.append(f"cannot read bundle: {e}")
    if bad:
        for b in bad:
            print(b, file=sys.stderr)
        return 1
    print("chartlib bundle ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]),
                    help="app checkout root (contains dashboards/)")
    ap.add_argument("--tarball-dir", metavar="DIR", help="use local <name>-<version>.tgz files")
    ap.add_argument("--dry-run", action="store_true", help="verify and report, write nothing")
    ap.add_argument("--check", action="store_true", help="verify the committed tree, exit 1 on a difference")
    args = ap.parse_args(argv)
    root = Path(args.root)
    if args.check:
        return _check(root)

    parts: dict[str, bytes] = {}
    licenses: dict[str, bytes] = {}
    for c in COMPONENTS:
        f, lic = _extract(c, _fetch(c, Path(args.tarball_dir) if args.tarball_dir else None))
        parts[c["file"]] = f
        licenses[c["license_out"]] = lic
    entry, bundle = build_entry(parts, licenses)

    manifest_path = root / "dashboards" / "vendor.json"
    doc = json.loads(manifest_path.read_text("utf-8"))
    libs = doc["libraries"]
    idx = next((i for i, x in enumerate(libs)
                if x.get("name") == BUNDLE_NAME and x.get("version") == BUNDLE_VERSION), None)
    if idx is None:
        libs.append(entry)
    else:
        libs[idx] = entry
    dest = root / "dashboards" / entry["dir"]
    if not args.dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / BUNDLE_FILE).write_bytes(bundle)
        for name, content in licenses.items():
            (dest / name).write_bytes(content)
        manifest_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {dest}; served_at {entry['served_at']}; sha256 {entry['files'][0]['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
