"""Phase 22 (DCAP-02): docs/dashboard-spec.v2.schema.json is generated from the models and pinned."""
import json
from pathlib import Path

from app import dashboardspec as ds

DOCS = Path(__file__).resolve().parents[2] / "docs"
V1 = DOCS / "dashboard-spec.v1.schema.json"
V2 = DOCS / "dashboard-spec.v2.schema.json"
FORBIDDEN = {"color", "colour", "colors", "palette", "css", "style", "script", "html", "code"}


def _norm(path: Path) -> bytes:
    # git may check the file out with CRLF on Windows; the content is what is pinned
    return path.read_bytes().replace(b"\r\n", b"\n")


def test_default_version_is_v1():
    assert ds.schema_dict() == ds.schema_dict(1) == ds.Spec.model_json_schema()
    assert ds.schema_text() == ds.schema_text(1)


def test_v2_text_shape():
    t = ds.schema_text(2)
    assert ds.schema_dict(2) == ds.SpecV2.model_json_schema()
    assert t.endswith("\n") and t == json.dumps(json.loads(t), sort_keys=True, indent=2) + "\n"
    assert '"discriminator"' in t and '"propertyName": "type"' in t


def test_committed_v2_schema_matches_models():
    assert _norm(V2) == ds.schema_text(2).encode("utf-8"), (
        "docs/dashboard-spec.v2.schema.json is stale; regenerate: cd backend && uv run --frozen "
        "python -c \"from app import dashboardspec; open('../docs/dashboard-spec.v2.schema.json',"
        "'w',newline='\\n').write(dashboardspec.schema_text(2))\"")


def test_committed_v1_schema_matches_models():
    assert _norm(V1) == ds.schema_text().encode("utf-8")


def _names(node, hits):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "properties" and isinstance(v, dict):
                hits.extend(n for n in v if n.lower() in FORBIDDEN)
            _names(v, hits)
    elif isinstance(node, list):
        for v in node:
            _names(v, hits)


def test_v2_schema_has_no_forbidden_property_names():
    hits: list = []
    _names(ds.schema_dict(2), hits)
    assert hits == []
    assert json.loads(_norm(V2))  # the committed file is valid JSON
