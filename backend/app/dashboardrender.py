"""Phase 18 (SPEC-02): pins for the fixed chart-spec renderer and the frame stylesheet.

Pure constants, stdlib only. The serving code builds the constant frame and the
hash-only CSP from this module. A test keeps every value here equal to
dashboards/vendor.json and to the vendored bytes (the backend image has no
dashboards/ directory), and scripts/pin_renderer.py --check does the same offline.
"""

import base64
import hashlib

CURRENT_RENDERER = "ic-render-1.0.0"

# Rewritten by scripts/pin_renderer.py; never edit by hand.
CSP_HASH_1_0_0 = "sha256-YbNWwj2W+vwtDj5/p3/uXyBtiFNdhV7e/4KAOlFf+dA="

# Rewritten by scripts/pin_renderer.py; never edit by hand.
CSP_HASH_2_0_0 = "sha256-6KBMppvU9IcLm4c7RbW9qochU+xRg2+fXeqGqKVSNDg="

# Chart.js 4.5.1 (vendor.json libraries[0]); a test keeps it equal to the manifest.
LIB_CSP_HASH = "sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="
LIB_SRC_1 = "/d/_lib/chart.js-4.5.1/chart.umd.min.js"
LIB_INTEGRITY_1 = LIB_CSP_HASH

# Phase 22 (DCAP-09): Chart.js 4.5.1 + annotation 3.1.0 + matrix 3.1.0 for ic-render-2.0.0
# (vendor.json ic-chartlib 1.0.0; a test keeps these equal to the manifest). The integrity
# attribute carries the sha256 token, so one value serves SRI and the CSP hash source.
CHARTLIB_SRC = "/d/_lib/ic-chartlib-1.0.0/chartlib.min.js"
CHARTLIB_CSP_HASH = "sha256-515ub9x4JPVTtBbkxrV9rLXo4+Qvz0LRoAy16ivCYy0="
CHARTLIB_INTEGRITY = CHARTLIB_CSP_HASH

# The single stylesheet of the frame. ASCII, no url(), no @import, no external fonts.
SPEC_CSS_1_0_0 = (
    "*{box-sizing:border-box}"
    "html,body{margin:0;background:#080C14;color:#F0F4F8;"
    "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;font-size:15px;line-height:1.4}"
    "#ic-root{max-width:1200px;margin:0 auto;padding:16px}"
    ".ic-title{margin:0 0 4px;font-size:22px;font-weight:650}"
    ".ic-sub{margin:0 0 4px;color:#A0AEC0}"
    ".ic-asof{margin:0 0 16px;font-size:12px;color:#A0AEC0}"
    ".ic-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-bottom:12px}"
    ".ic-tile{background:#0E1420;border:1px solid #1E2D42;border-radius:8px;padding:12px;min-width:0}"
    ".ic-tile-title{margin:0 0 8px;font-size:13px;font-weight:600;color:#A0AEC0;"
    "text-transform:uppercase;letter-spacing:.04em}"
    ".ic-note{margin:8px 0 0;font-size:12px;color:#A0AEC0}"
    ".ic-stat{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px}"
    ".ic-stat-value{font-size:32px;font-weight:650;font-variant-numeric:tabular-nums}"
    ".ic-stat-period{font-size:12px;color:#A0AEC0}"
    ".ic-delta{font-size:13px;font-variant-numeric:tabular-nums;color:#A0AEC0}"
    ".ic-good{color:#3FB27F}"
    ".ic-bad{color:#E05252}"
    ".ic-flat{color:#A0AEC0}"
    ".ic-spark{position:relative;height:40px;margin-top:8px}"
    ".ic-chart{position:relative;height:260px}"
    ".ic-chart-tall{height:340px}"
    ".ic-table-wrap{overflow-x:auto}"
    ".ic-table{width:100%;border-collapse:collapse;font-size:13px}"
    ".ic-table th{text-align:left;color:#A0AEC0;font-weight:600;padding:6px 8px;border-bottom:1px solid #1E2D42}"
    ".ic-table td{padding:6px 8px;border-bottom:1px solid #1E2D42}"
    ".ic-num{text-align:right;font-variant-numeric:tabular-nums}"
    ".ic-more{margin:6px 0 0;font-size:12px;color:#A0AEC0}"
    ".ic-text{margin:0;white-space:pre-wrap}"
    ".ic-empty{margin:8px 0 0;color:#A0AEC0}"
    ".ic-error,.ic-stale,.ic-fail,.ic-owed{color:#D4952B}"
    ".ic-error,.ic-stale{margin:8px 0 0;font-size:13px}"
    ".ic-fail{padding:24px 0;font-size:16px}"
    "@media (max-width:640px){.ic-row{grid-template-columns:1fr}.ic-stat-value{font-size:28px}}"
)


# ---- Phase 22 (DCAP-02, DCAP-16): the ic-render-2.0.0 frame stylesheet (UI-SPEC sections 1-5, 9-14).
# Same colours as THEMES in render.js. Tokens: dark by default, light under prefers-color-scheme,
# light for print.
_TOKENS_DARK = (
    "--ic-page:#080C14;--ic-surface:#0E1420;--ic-border:#1E2D42;--ic-grid:#1E2D42;"
    "--ic-ink:#F0F4F8;--ic-ink-2:#A0AEC0;--ic-muted:#8A96A6;--ic-good:#3FB27F;--ic-bad:#E05252;"
    "--ic-owed:#D4952B;--ic-inward:#3987E5;--ic-outward:#B8409A;--ic-neutral:#5C6375;"
    "--ic-accent:#1F9E93;")
_TOKENS_LIGHT = (
    "--ic-page:#F5F6F8;--ic-surface:#FFFFFF;--ic-border:#DDE1E7;--ic-grid:#E6E8EC;"
    "--ic-ink:#14181F;--ic-ink-2:#4A5463;--ic-muted:#6B7684;--ic-good:#1A7F37;--ic-bad:#C62F2F;"
    "--ic-owed:#9A6100;--ic-inward:#2A78D6;--ic-outward:#9A4F94;--ic-neutral:#445160;"
    "--ic-accent:#0AA19B;")


def _span(cls: str, n: int) -> str:
    return f".ic-w-{cls}{{grid-column:span {n}}}"


SPEC_CSS_2_0_0 = (
    ":root{" + _TOKENS_DARK + "color-scheme:dark}"
    "@media (prefers-color-scheme: light){:root{" + _TOKENS_LIGHT + "color-scheme:light}}"
    "*{box-sizing:border-box}"
    "html,body{margin:0;background:var(--ic-page);color:var(--ic-ink);"
    "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;line-height:1.4}"
    "#ic-root{max-width:1280px;margin:0 auto;padding:12px}"
    ".ic-head{margin:0 0 16px}"
    ".ic-title{margin:0 0 4px;font-size:22px;font-weight:650}"
    ".ic-sub{margin:0 0 4px;color:var(--ic-ink-2)}"
    ".ic-asof{margin:0 0 8px;font-size:12px;color:var(--ic-muted)}"
    ".ic-bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;position:relative}"
    "button,select,input{font:inherit;color:inherit}"
    ".ic-range,.ic-preset,.ic-apply,.ic-close,.ic-chip-x,.ic-section-toggle,.ic-sort,.ic-prev,.ic-next,.ic-info{"
    "background:var(--ic-surface);border:1px solid var(--ic-border);border-radius:6px;cursor:pointer}"
    ".ic-range,.ic-preset,.ic-apply,.ic-close{min-height:40px;min-width:40px;padding:6px 12px}"
    ".ic-sheet{position:fixed;left:0;right:0;bottom:0;width:100%;max-height:80%;overflow:auto;z-index:10;"
    "background:var(--ic-surface);border:1px solid var(--ic-border);border-radius:8px 8px 0 0;padding:12px;"
    "display:flex;flex-direction:column;gap:8px}"
    ".ic-preset.ic-on{border-color:var(--ic-inward);color:var(--ic-inward);font-weight:600}"
    ".ic-date{min-height:40px;padding:6px 8px;background:var(--ic-page);border:1px solid var(--ic-border);"
    "border-radius:6px}"
    ".ic-chip{display:inline-flex;align-items:center;gap:4px;min-height:40px;padding:0 4px 0 10px;"
    "border:1px solid var(--ic-border);border-radius:6px;font-size:12px;color:var(--ic-ink-2)}"
    ".ic-chip-x{min-height:40px;min-width:40px;padding:0 8px}"
    ".ic-filter{min-height:40px;padding:6px 8px;background:var(--ic-surface);"
    "border:1px solid var(--ic-border);border-radius:6px}"
    ".ic-section{margin:0 0 24px}"
    ".ic-section-head{display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--ic-border);"
    "margin:0 0 12px;padding:0 0 6px}"
    ".ic-section-title{margin:0;font-size:15px;font-weight:600;color:var(--ic-ink)}"
    ".ic-section-desc{margin:2px 0 0;font-size:12px;color:var(--ic-muted)}"
    ".ic-section-toggle{display:none;min-height:40px;padding:4px 10px;margin-left:auto}"
    ".ic-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}"
    ".ic-w-quarter{grid-column:span 1}"
    ".ic-w-third,.ic-w-half,.ic-w-two_thirds,.ic-w-full{grid-column:span 2}"
    ".ic-tile{background:var(--ic-surface);border:1px solid var(--ic-border);border-radius:8px;padding:12px;"
    "min-width:0}"
    ".ic-tile-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 8px}"
    ".ic-tile-title{margin:0;font-size:13px;font-weight:600;color:var(--ic-ink-2)}"
    ".ic-tile-asof,.ic-now,.ic-fixed{font-size:12px;color:var(--ic-muted)}"
    ".ic-tile-head>span{display:inline-flex;align-items:center;gap:8px}"
    ".ic-info{width:24px;height:24px;padding:0;font-size:12px;position:relative}"
    ".ic-def{margin:0 0 8px;font-size:12px;color:var(--ic-ink-2)}"
    ".ic-note,.ic-basis,.ic-legend-note{margin:8px 0 0;font-size:12px;color:var(--ic-muted)}"
    ".ic-stat{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px}"
    ".ic-stat-value{font-size:28px;font-weight:650;font-variant-numeric:tabular-nums}"
    ".ic-stat-period{font-size:12px;color:var(--ic-muted)}"
    ".ic-delta{font-size:13px;font-variant-numeric:tabular-nums;color:var(--ic-muted)}"
    ".ic-good{color:var(--ic-good)}"
    ".ic-bad{color:var(--ic-bad)}"
    ".ic-flat{color:var(--ic-muted)}"
    ".ic-owed{color:var(--ic-owed)}"
    ".ic-owed-badge{display:inline-block;margin-left:6px;padding:0 6px;border:1px solid var(--ic-owed);"
    "border-radius:6px;font-size:12px;color:var(--ic-owed)}"
    ".ic-spark{position:relative;height:40px;margin-top:8px}"
    ".ic-chart{position:relative;height:220px}"
    ".ic-chart-tall{position:relative;height:340px}"
    ".ic-bullet{position:relative;height:64px}"
    ".ic-stat+.ic-bullet{height:6px;margin-top:8px;border-radius:3px;overflow:hidden;background:var(--ic-grid)}"
    ".ic-table-wrap{overflow-x:auto}"
    ".ic-table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}"
    ".ic-table th{text-align:left;color:var(--ic-ink-2);font-weight:600;padding:6px 8px;"
    "border-bottom:1px solid var(--ic-border)}"
    ".ic-table td{padding:6px 8px;border-bottom:1px solid var(--ic-border)}"
    ".ic-table th:first-child,.ic-table td:first-child{position:sticky;left:0;background:var(--ic-surface)}"
    ".ic-num{text-align:right;font-variant-numeric:tabular-nums}"
    ".ic-sort{border:0;background:transparent;padding:0;font-weight:600;color:var(--ic-ink-2)}"
    ".ic-total td,.ic-total th{font-weight:600;border-top:1px solid var(--ic-border)}"
    ".ic-pager{display:flex;align-items:center;gap:8px;margin:8px 0 0;font-size:12px;color:var(--ic-muted)}"
    ".ic-prev,.ic-next{min-height:32px;padding:2px 10px}"
    ".ic-cellbar{display:block;height:6px;border-radius:3px;background:var(--ic-inward);opacity:.35}"
    ".ic-text{margin:0;white-space:pre-wrap}"
    ".ic-empty,.ic-na{margin:8px 0 0;color:var(--ic-muted)}"
    ".ic-error,.ic-stale{margin:8px 0 0;font-size:13px;color:var(--ic-owed)}"
    ".ic-fail{padding:24px 0;font-size:16px;color:var(--ic-owed)}"
    ".ic-sr{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;"
    "clip:rect(0,0,0,0);white-space:nowrap;border:0}"
    "@media (min-width:640px){"
    ".ic-grid{grid-template-columns:repeat(6,minmax(0,1fr))}"
    + _span("quarter", 3) + _span("third", 3) + _span("half", 3) + _span("two_thirds", 6) + _span("full", 6)
    + ".ic-sheet{position:absolute;top:100%;left:0;right:auto;bottom:auto;width:320px;max-height:none;"
    "border-radius:8px}"
    ".ic-chart{height:260px}"
    ".ic-stat-value{font-size:32px}"
    "}"
    "@media (min-width:768px){"
    ".ic-grid{gap:16px}.ic-tile{padding:16px}#ic-root{padding:16px}"
    "}"
    "@media (min-width:1024px){"
    ".ic-grid{grid-template-columns:repeat(12,minmax(0,1fr))}"
    + _span("quarter", 3) + _span("third", 4) + _span("half", 6) + _span("two_thirds", 8) + _span("full", 12)
    + "}"
    "@media (max-width:639px){"
    ".ic-section-toggle{display:inline-block}"
    ".ic-section.ic-collapsed .ic-grid{display:none}"
    ".ic-filter{width:100%}"
    "}"
    "@media print{"
    ":root{" + _TOKENS_LIGHT + "color-scheme:light}"
    ".ic-bar,.ic-sheet,.ic-info,.ic-pager,.ic-chip-x,.ic-section-toggle{display:none}"
    ".ic-section.ic-collapsed .ic-grid{display:grid}"
    ".ic-tile{break-inside:avoid}.ic-section{break-inside:avoid}"
    "html,body{background:#FFFFFF}"
    "}"
)


def _sha256_token(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


RENDERERS = {
    CURRENT_RENDERER: {
        "src": "/d/_lib/ic-render-1.0.0/render.js",
        "csp_hash": CSP_HASH_1_0_0,
        "css": SPEC_CSS_1_0_0,
        "css_hash": _sha256_token(SPEC_CSS_1_0_0),
        "lib_src": LIB_SRC_1,
        "lib_integrity": LIB_INTEGRITY_1,
        "lib_csp_hash": LIB_CSP_HASH,
    },
    "ic-render-2.0.0": {
        "src": "/d/_lib/ic-render-2.0.0/render.js",
        "csp_hash": CSP_HASH_2_0_0,
        "css": SPEC_CSS_2_0_0,
        "css_hash": _sha256_token(SPEC_CSS_2_0_0),
        "lib_src": CHARTLIB_SRC,
        "lib_integrity": CHARTLIB_INTEGRITY,
        "lib_csp_hash": CHARTLIB_CSP_HASH,
    },
}

_RENDERER_FOR_VERSION = {1: "ic-render-1.0.0", 2: "ic-render-2.0.0"}

SPEC_CSS = RENDERERS[CURRENT_RENDERER]["css"]


def renderer_for(spec_version: int) -> str | None:
    """The renderer name that draws a spec version, or None for an unknown version."""
    return _RENDERER_FOR_VERSION.get(spec_version)


def pin(name: str) -> dict | None:
    """The renderer pin (src, csp_hash, css, css_hash) for a renderer name, or None."""
    return RENDERERS.get(name)
