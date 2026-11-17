"""Phase 2 — triage parsing helpers (pure; no DB/LLM)."""

from datetime import timezone

from app.triage import _extract_json, _parse_due


def test_extract_json_from_fenced_or_noisy_text():
    assert _extract_json('```json\n{"verdicts": []}\n```') == {"verdicts": []}
    assert _extract_json('sure, here:\n{"a": 1} \n\ndone') == {"a": 1}
    assert _extract_json("no json here") is None
    assert _extract_json("") is None


def test_extract_json_handles_anthropic_content_blocks():
    blocks = [{"type": "text", "text": '{"verdicts": [{"event_id": 1}]}'}]
    assert _extract_json(blocks) == {"verdicts": [{"event_id": 1}]}


def test_parse_due_accepts_iso_and_z_suffix():
    dt = _parse_due("2026-03-20T17:00:00Z")
    assert dt is not None and dt.tzinfo is not None
    assert dt.astimezone(timezone.utc).hour == 17
    # naive input gets a UTC tzinfo attached
    assert _parse_due("2026-03-20T17:00:00").tzinfo is not None


def test_parse_due_rejects_garbage():
    assert _parse_due("next friday") is None
    assert _parse_due(None) is None
    assert _parse_due("") is None
