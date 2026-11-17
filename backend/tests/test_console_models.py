"""Phase 13 (GOLIVE-01): the fallback console's model datalist follows the catalog."""
import re
from pathlib import Path

from app import llmclient

HTML = (Path(__file__).resolve().parent.parent / "app" / "dashboard.html").read_text(encoding="utf-8")


def _datalist() -> set[str]:
    m = re.search(r'<datalist id="model-options">(.*?)</datalist>', HTML, re.S)
    assert m
    return set(re.findall(r'value="([^"]+)"', m.group(1)))


def test_console_datalist_matches_current_models():
    claude = {v for v in _datalist() if v.startswith("claude-")}
    assert claude == {m["value"] for m in llmclient.CURRENT_MODELS}


def test_console_datalist_has_no_retired_defaults():
    vals = _datalist()
    assert "claude-sonnet-4-5" not in vals and "claude-haiku-4-5" not in vals
