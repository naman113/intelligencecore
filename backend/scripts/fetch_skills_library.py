#!/usr/bin/env python3
"""Vendor the skills_library/ tree from upstream skill collections.

DEV-TIME ONLY. This script runs on the developer machine and needs the `git`
CLI plus network access; its output (skills_library/ + manifest.json at the
repo root) is committed. The gateway NEVER fetches — at runtime the library is
read-only committed content, and it may be absent entirely (skillservice fails
soft to an empty library).

Self-contained on purpose: stdlib only (yaml is used if importable, with a
regex fallback), zero imports from app/ — it must run outside the backend
virtualenv.

Sources (order matters — slug collisions dedupe first-in-wins):
  anthropics  https://github.com/anthropics/skills           (skills/ subtree)
  hermes      https://github.com/NousResearch/hermes-agent   (skills/ subtree)
  opencode    https://github.com/farmage/opencode-skills     (repo root)

Per skill: parse SKILL.md frontmatter, resolve a license (skill-dir LICENSE*
→ nearest parent → repo root; text sniffed to SPDX; skipped unless in the
allowlist), classify compat ('coding-agent' when the body smells of
terminals/repos, else 'general' — crude, honest), copy files <=200KB
preserving subdirs, and write a manifest entry. Anthropics' document skills
(docx/pdf/pptx/xlsx) are hard-skipped. Manifest `path` values are RELATIVE
with FORWARD slashes: authored on Windows, read inside a Linux container.
Total library size is budgeted at ~10MB — the script stops adding and says so.

Usage:
  python fetch_skills_library.py [--dry-run] [--only SOURCE]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEST = REPO_ROOT / "skills_library"

# source -> (clone URL, subtree holding skill dirs; "" = repo root)
SOURCES: dict[str, tuple[str, str]] = {
    "anthropics": ("https://github.com/anthropics/skills", "skills"),
    "hermes": ("https://github.com/NousResearch/hermes-agent", "skills"),
    "opencode": ("https://github.com/farmage/opencode-skills", ""),
}

LICENSE_ALLOW = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
                 "CC-BY-4.0"}
# Anthropics' office-document skills: heavyweight bundled tooling, separate
# licensing story — never vendored, regardless of what a license sniff says.
HARD_SKIP: dict[str, set[str]] = {"anthropics": {"docx", "pdf", "pptx", "xlsx"}}

FILE_MAX = 204800                 # per-file cap (skill_asset's ASSET_MAX)
BODY_MAX = 65536                  # SKILL.md cap (skillservice.MAX_BODY)
TOTAL_MAX = 10 * 1024 * 1024      # ~10MB library budget
# Per-skill ceiling: one asset-heavy skill (canvas-design ships 5.5MB of
# fonts) would otherwise eat half the budget and crowd out dozens of small
# skills — breadth beats one bundle in a browsable catalog.
SKILL_MAX = 400 * 1024
RESERVED = {"library", "install"}
SLUG_RE = re.compile(r"[a-z0-9-]{1,64}$")

CODING_HINTS = ("terminal", "bash", "shell", " git", "npm", "pytest", "cli",
                "ide ", "codebase", "repository", "command line", "pip ")

# Hand-curated: skills with a plausible job in THIS system (a WhatsApp
# business assistant that answers with read-only SQL, extracts/generates
# documents, runs pandas analyses in a sandbox, watches things via playbooks,
# and writes business communications). Everything else in the catalog is
# browsable but tagged relevant=false so the UIs can default to the shelf
# that matters. Curated by hand on 2026-08-12 — revisit when re-vendoring.
RELEVANT_SLUGS = {
    "anthropics/skill-creator",        # authoring our own skills
    "anthropics/internal-comms",       # business communication formats
    "hermes/humanizer",                # outbound tone: strip AI-isms
    "hermes/document-to-action-items", # obligations/deadlines → cases
    "hermes/meeting-action-items",     # meeting notes → decisions/owners
    "hermes/email-inbox-triage",       # triage judgment (tool-agnostic)
    "hermes/competitor-news-monitor",  # web-research playbook procedure
    "hermes/product-price-monitor",    # watch/alert playbook procedure
    "hermes/weekly-review-planning",   # weekly digest/review playbook
    "hermes/grounded-citations",       # cited, verifiable answers
    "hermes/ocr-and-documents",        # PDF/scan text extraction craft
    "opencode/pandas-pro",             # Analysis agent's sandbox pandas
    "opencode/sql-pro",                # Data agent's SQL craft
    "intelligence-corner/dashboard-authoring",  # Phase 5: first-party house style (kept relevant across --reclassify)
    "intelligence-corner/exploratory-data-analysis",  # Phase 7: first-party exploration skill (kept relevant across --reclassify)
    "intelligence-corner/kpi-selection",  # Phase 7: first-party exploration skill (kept relevant across --reclassify)
    "intelligence-corner/forecasting",  # Quick 004: first-party analytics skill (kept relevant across --reclassify)
    "intelligence-corner/statistical-tests",  # Quick 004: first-party analytics skill (kept relevant across --reclassify)
    "intelligence-corner/anomaly-detection",  # Quick 004: first-party analytics skill (kept relevant across --reclassify)
    "intelligence-corner/trend-and-seasonality",  # Quick 004: first-party analytics skill (kept relevant across --reclassify)
    "intelligence-corner/driver-analysis",  # Quick 004: first-party analytics skill (kept relevant across --reclassify)
}

BOM = chr(0xFEFF)  # U+FEFF, shipped by some Windows editors ahead of '---'


# ── minimal SKILL.md frontmatter parsing (self-contained; no app imports) ──

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter dict, body). yaml.safe_load when available, else a regex
    `key: value` fallback. Unclosed fences are treated as no frontmatter."""
    # Upstream repos may check out with CRLF on Windows (this script's usual
    # host) and some ship a BOM; either would defeat fence detection below.
    text = text.lstrip(BOM).replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---"):
        return {}, text
    first_nl = text.find("\n")
    if first_nl == -1 or text[:first_nl].strip() != "---":
        return {}, text
    close = re.search(r"\n---[ \t]*(\n|$)", text[first_nl:])
    if not close:
        return {}, text
    raw = text[first_nl + 1: first_nl + close.start()]
    body = text[first_nl + close.end():].lstrip("\n")
    fm: dict = {}
    try:
        import yaml  # optional; PyYAML is common but not required
        parsed = yaml.safe_load(raw)
        if isinstance(parsed, dict):
            fm = parsed
    except Exception:
        for line in raw.splitlines():
            m = re.match(r"^([A-Za-z][\w-]*)\s*:\s*(.+?)\s*$", line)
            if m and not m.group(2).startswith("!"):  # never honor YAML tags
                fm[m.group(1)] = m.group(2).strip("'\"")
    return fm, body


def sanitize_desc(s: str) -> str:
    s = re.sub(r"[#`<>]", "", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:200]


def describe(fm: dict, body: str) -> str:
    d = fm.get("description")
    if isinstance(d, str) and d.strip():
        return sanitize_desc(d)
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if para and not para.startswith("#"):
            return sanitize_desc(para)
    return ""


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", str(name or "").lower()).strip("-")[:64]
    return s.strip("-")


# ── license resolution ─────────────────────────────────────────────────────

_SPDX_SNIFFS = [
    ("Apache-2.0", ("apache license", "version 2.0")),
    ("BSD-3-Clause", ("redistribution and use", "neither the name")),
    ("BSD-2-Clause", ("redistribution and use", "this list of conditions")),
    ("CC-BY-4.0", ("creative commons attribution 4.0",)),
    ("MIT", ("mit license",)),
    ("MIT", ("permission is hereby granted, free of charge",)),
]


def spdx_from_text(text: str) -> str | None:
    low = " ".join(text.lower().split())
    for spdx, needles in _SPDX_SNIFFS:
        if all(n in low for n in needles):
            return spdx
    return None


def resolve_license(skill_dir: Path, clone_root: Path, fm: dict) -> str | None:
    """skill dir LICENSE* → nearest parent → clone root; frontmatter `license`
    accepted only when it already names an allowlisted SPDX id."""
    lic = fm.get("license")
    if isinstance(lic, str) and lic.strip() in LICENSE_ALLOW:
        return lic.strip()
    d = skill_dir
    while True:
        for f in sorted(d.glob("LICENSE*")) + sorted(d.glob("COPYING*")):
            if f.is_file():
                try:
                    spdx = spdx_from_text(f.read_text(encoding="utf-8",
                                                      errors="replace"))
                except OSError:
                    spdx = None
                if spdx:
                    return spdx
        if d == clone_root:
            return None
        d = d.parent
        if clone_root not in (d, *d.parents):
            return None


def classify_compat(body: str) -> str:
    low = body.lower()
    return "coding-agent" if any(h in low for h in CODING_HINTS) else "general"


# ── vendoring ──────────────────────────────────────────────────────────────

def clone(url: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="skills-clone-"))
    subprocess.run(["git", "clone", "--depth", "1", url, str(tmp)],
                   check=True, capture_output=True, text=True)
    return tmp


def copy_skill(skill_dir: Path, dest_dir: Path, dry_run: bool) -> tuple[int, list[str]]:
    """Copy files <=FILE_MAX preserving subdirs. Returns (bytes, notes)."""
    total = 0
    notes: list[str] = []
    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file() or ".git" in f.parts:
            continue
        size = f.stat().st_size
        rel = f.relative_to(skill_dir)
        if size > FILE_MAX:
            notes.append(f"skipped {rel.as_posix()} ({size}B > {FILE_MAX}B)")
            continue
        if not dry_run:
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
        total += size
    return total, notes


def process_source(source: str, clone_root: Path, subtree: str,
                   taken_slugs: dict[str, str], manifest: list[dict],
                   running_total: int, dry_run: bool) -> tuple[int, list[tuple]]:
    """Vendor one source. Returns (new running_total, report rows:
    (source, slug, status, reason))."""
    rows: list[tuple] = []
    base = clone_root / subtree if subtree else clone_root
    if not base.is_dir():
        return running_total, [(source, "-", "error", f"subtree {subtree!r} missing")]

    skill_dirs = sorted({p.parent for p in base.rglob("SKILL.md")})
    for skill_dir in skill_dirs:
        raw_name = skill_dir.name
        slug = slugify(raw_name)
        if raw_name in HARD_SKIP.get(source, set()) or slug in HARD_SKIP.get(source, set()):
            rows.append((source, slug or raw_name, "skipped", "hard-skip (document skill)"))
            continue
        if not slug or not SLUG_RE.fullmatch(slug) or slug in RESERVED:
            rows.append((source, raw_name, "skipped", f"unusable slug {slug!r}"))
            continue
        if slug in taken_slugs:
            rows.append((source, slug, "skipped",
                         f"slug collision — kept {taken_slugs[slug]} (first-in-wins)"))
            continue

        try:
            text = (skill_dir / "SKILL.md").read_text(encoding="utf-8",
                                                      errors="replace")
        except OSError as e:
            rows.append((source, slug, "skipped", f"unreadable SKILL.md: {e}"))
            continue
        if len(text.encode("utf-8", errors="replace")) > BODY_MAX:
            rows.append((source, slug, "skipped", f"SKILL.md > {BODY_MAX}B"))
            continue

        fm, body = parse_frontmatter(text)
        license_ = resolve_license(skill_dir, clone_root, fm)
        if license_ not in LICENSE_ALLOW:
            rows.append((source, slug, "skipped",
                         f"license {license_ or 'unresolved'} not allowlisted"))
            continue

        skill_bytes = sum(f.stat().st_size for f in skill_dir.rglob("*")
                          if f.is_file() and f.stat().st_size <= FILE_MAX
                          and ".git" not in f.parts)
        if skill_bytes > SKILL_MAX:
            rows.append((source, slug, "skipped",
                         f"skill {skill_bytes}B > {SKILL_MAX // 1024}KB per-skill cap"))
            continue
        if running_total + skill_bytes > TOTAL_MAX:
            rows.append((source, slug, "skipped",
                         f"~{TOTAL_MAX // (1024 * 1024)}MB library budget reached"))
            continue

        dest_dir = DEST / source / slug
        copied, notes = copy_skill(skill_dir, dest_dir, dry_run)
        running_total += copied
        taken_slugs[slug] = f"{source}/{slug}"
        manifest.append({
            "id": f"{source}/{slug}",
            "name": slug,
            "description": describe(fm, body),
            "source": source,
            "license": license_,
            "compat": classify_compat(body),
            "relevant": f"{source}/{slug}" in RELEVANT_SLUGS,
            # RELATIVE + FORWARD slashes: authored on Windows, read on Linux.
            "path": f"{source}/{slug}",
        })
        note = f"; {len(notes)} oversize file(s) skipped" if notes else ""
        rows.append((source, slug, "imported", f"{copied}B{note}"))
        for n in notes:
            rows.append((source, slug, "note", n))
    return running_total, rows


def write_manifest(manifest: list[dict]) -> None:
    import json
    DEST.mkdir(parents=True, exist_ok=True)
    (DEST / "manifest.json").write_text(
        json.dumps(sorted(manifest, key=lambda e: e["id"]), indent=2,
                   ensure_ascii=False) + "\n",
        encoding="utf-8")


def load_existing_manifest() -> list[dict]:
    import json
    try:
        data = json.loads((DEST / "manifest.json").read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Vendor skills_library/ from upstream skill repos "
                    "(dev machine only; the gateway never fetches).")
    ap.add_argument("--dry-run", action="store_true",
                    help="clone and report, but write nothing")
    ap.add_argument("--only", choices=sorted(SOURCES),
                    help="vendor a single source, preserving the others' "
                         "manifest entries")
    ap.add_argument("--reclassify", action="store_true",
                    help="no cloning: rewrite relevant/compat on the existing "
                         "manifest from the current curation rules")
    args = ap.parse_args(argv)

    if args.reclassify:
        manifest = load_existing_manifest()
        if not manifest:
            print("no existing manifest to reclassify", file=sys.stderr)
            return 1
        for e in manifest:
            e["relevant"] = e.get("id") in RELEVANT_SLUGS
        write_manifest(manifest)
        n = sum(1 for e in manifest if e["relevant"])
        print(f"reclassified {len(manifest)} entries; {n} relevant")
        return 0

    selected = [args.only] if args.only else list(SOURCES)
    # Preserve non-selected sources' entries; rebuild the selected ones.
    manifest = [e for e in load_existing_manifest()
                if e.get("source") not in selected]
    taken_slugs = {e["name"]: e["id"] for e in manifest if "name" in e}
    running_total = 0
    if DEST.is_dir():
        running_total = sum(
            f.stat().st_size for f in DEST.rglob("*")
            if f.is_file() and f.name != "manifest.json"
            and f.relative_to(DEST).parts[0] not in selected)

    all_rows: list[tuple] = []
    tmp_dirs: list[Path] = []
    try:
        for source in selected:
            url, subtree = SOURCES[source]
            if not args.dry_run:
                target = DEST / source
                if target.is_dir():
                    shutil.rmtree(target)
            print(f"cloning {source} ({url}) ...", flush=True)
            try:
                root = clone(url)
            except subprocess.CalledProcessError as e:
                all_rows.append((source, "-", "error",
                                 f"git clone failed: {e.stderr.strip()[:200]}"))
                continue
            tmp_dirs.append(root)
            running_total, rows = process_source(
                source, root, subtree, taken_slugs, manifest,
                running_total, args.dry_run)
            all_rows.extend(rows)

        if not args.dry_run:
            write_manifest(manifest)
    finally:
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    # ── report ──
    width = max([len(r[1]) for r in all_rows] + [8])
    print(f"\n{'source':<12} {'skill':<{width}} {'status':<9} detail")
    print("-" * (12 + width + 10 + 40))
    for source, slug, status, reason in all_rows:
        print(f"{source:<12} {slug:<{width}} {status:<9} {reason}")
    imported = sum(1 for r in all_rows if r[2] == "imported")
    skipped = sum(1 for r in all_rows if r[2] == "skipped")
    print(f"\n{imported} imported, {skipped} skipped; "
          f"library ≈ {running_total}B"
          + (" (dry run — nothing written)" if args.dry_run else
             f"; manifest: {DEST / 'manifest.json'}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
