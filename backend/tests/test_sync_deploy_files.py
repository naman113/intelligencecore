"""Phase 15 SRC-05/SRC-08 static guards: internal sync network, sync image builds, SYNC_* docs."""

import re
from pathlib import Path

import yaml

APP = Path(__file__).resolve().parents[2]
BASE = APP / "docker-compose.yml"
LOCAL = APP / "docker-compose.local.yml"


class _Loader(yaml.SafeLoader):
    pass


def _tag(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_Loader.add_constructor("!override", _tag)
_Loader.add_constructor("!reset", _tag)


def _load(p: Path) -> dict:
    return yaml.load(p.read_text(encoding="utf-8"), Loader=_Loader)


def _nets(svc: dict) -> list:
    n = svc.get("networks") or []
    return list(n.keys()) if isinstance(n, dict) else list(n)


def test_base_warehouse_sync_internal():
    d = _load(BASE)
    assert _nets(d["services"]["warehouse"]) == ["default", "sync_internal", "sync_egress"]
    net = d["networks"]["sync_internal"]
    assert net["internal"] is True
    assert "name" not in net


def test_sync_internal_only_warehouse():
    d = _load(BASE)
    others = [n for n, s in d["services"].items() if n != "warehouse" and "sync_internal" in _nets(s)]
    assert others == []


def test_local_overlay_sync_env():
    base, local = _load(BASE), _load(LOCAL)
    wh = dict(base["services"]["warehouse"])
    wh.update({k: v for k, v in (local["services"].get("warehouse") or {}).items() if k == "networks"})
    assert {"sync_internal", "sync_egress", "default"} <= set(_nets(wh))
    env = local["services"]["app"]["environment"]
    assert env["SYNC_IMAGE"] == "agentic-sync:latest"
    assert env["SYNC_WAREHOUSE_HOST"] == base["services"]["warehouse"]["container_name"]  # Quick 031
    assert not env.get("SYNC_NETWORK") and not env.get("SYNC_CSV_NETWORK")


def test_install_and_setup_build_sync_image():
    for name in ("install.sh", "setup.sh"):
        lines = (APP / name).read_text(encoding="utf-8").splitlines()
        builds = [l for l in lines if "docker build" in l and "agentic-sync:latest" in l]
        assert len(builds) == 1, name
        assert "||" in builds[0], name
        assert any("infra/ic-sync" in l and "-d" in l for l in lines), name


def test_agentctl_sync_build_calls():
    t = (APP / "deploy" / "agentctl").read_text(encoding="utf-8")
    assert "_p15_sync_build()" in t
    assert "agentic-sync:latest" in t
    calls = re.findall(r"_p14_wh_up\s+#[^\n]*\n\s*_p15_sync_build", t)
    assert len(calls) == 3


def test_env_example_sync_section():
    t = (APP / ".env.example").read_text(encoding="utf-8")
    for k in ("SYNC_IMAGE", "SYNC_NETWORK", "SYNC_CSV_NETWORK", "SYNC_WAREHOUSE_HOST", "SYNC_MEMORY", "SYNC_CPUS"):
        assert k in t
