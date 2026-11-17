"""Markdown -> WhatsApp normalization at the send chokepoint.

The gateway shipped a research reply full of '##' headers, '---' rules and
'**bold**'. WhatsApp renders none of those, so the user read literal
punctuation. Twelve modules generate outbound text, each with its own prompt, so
the fix lives in whatsapp.send_message rather than in one prompt.

What is worth testing here, and why:
  * each transform, because getting one regex line-anchored wrong silently
    mangles ordinary prose (a mid-sentence '#' or a lone '*' must survive);
  * fenced blocks passing through, because ``` IS valid WhatsApp syntax and
    rewriting its contents would corrupt the one thing already correct;
  * idempotence, since a retry or any future second pass must not turn *x* into
    something else;
  * that a formatting failure never costs a send;
  * and above all that send_message normalizes BEFORE _record_self. Those two
    strings must be identical: connectors._is_self_echo matches on exact
    content, so a divergence stops the Baileys echo from matching and the
    assistant's own message enters triage as an external event — the loop the
    provenance='self' invariant exists to prevent.
"""

import types

from app import whatsapp
from app.whatsapp import for_whatsapp as f

# No pytestmark: asyncio_mode = "auto" already runs the async tests below, and a
# module-level mark warns on every sync test in the file.


# ---------------------------------------------------------------- transforms

def test_headings_become_whatsapp_bold():
    assert f("## Legal & Incorporation") == "*Legal & Incorporation*"
    assert f("# Top") == "*Top*"
    assert f("###### Deep") == "*Deep*"


def test_heading_that_is_already_bold_does_not_double_up():
    # "## **X**" must not come back out as "**X**" — the thing being removed.
    assert f("## **Business Overview**") == "*Business Overview*"


def test_horizontal_rules_are_dropped():
    assert f("a\n---\nb") == "a\nb"
    assert f("a\n***\nb") == "a\nb"
    assert f("a\n___\nb") == "a\nb"


def test_double_asterisk_bold_becomes_single():
    assert f("**Revenue:** 368 Cr") == "*Revenue:* 368 Cr"
    assert f("__Revenue__ rose") == "*Revenue* rose"


def test_markdown_links_become_text_then_url():
    assert f("see [Tofler](https://tofler.in/x)") == "see Tofler (https://tofler.in/x)"


def test_star_and_plus_bullets_become_dashes():
    # '* item' would otherwise read as an unclosed bold marker on WhatsApp.
    assert f("* one\n+ two") == "- one\n- two"


def test_tables_flatten_to_lines_and_separators_vanish():
    table = ("| Field | Value |\n"
             "|-------|-------|\n"
             "| CIN | U74999 |\n"
             "| Revenue | 368 Cr |")
    assert f(table) == "Field — Value\nCIN — U74999\nRevenue — 368 Cr"


def test_runs_of_blank_lines_collapse():
    assert f("a\n\n\n\n\nb") == "a\n\nb"


# ---------------------------------------------------------------- must not touch

def test_fenced_blocks_pass_through_untouched():
    src = "before\n```\n## not a heading\n**not bold**\n| a | b |\n```\nafter"
    assert f(src) == src


def test_plain_text_is_returned_byte_for_byte():
    for plain in (
        "Reminder: please send the fuel sheet by Friday.",
        "Rate is 5 * 3 per unit",              # incidental asterisk
        "See ticket #4417 for details",        # mid-line hash
        "a - b - c",                           # dashes that are not a rule
        "⏳ On it — this one needs some processing, ~a minute.",
    ):
        assert f(plain) == plain


def test_single_asterisk_emphasis_survives():
    # Already WhatsApp syntax; rewriting it is what breaks idempotence.
    assert f("*Aryan Pumps* is a client") == "*Aryan Pumps* is a client"


# ---------------------------------------------------------------- robustness

def test_idempotent():
    src = ("## Profile\n\n**CIN:** U74999\n\n---\n\n* one\n* two\n"
           "| a | b |\n|---|---|\n| 1 | 2 |\nsee [x](https://y.z)")
    once = f(src)
    assert f(once) == once


def test_malformed_input_does_not_raise():
    for bad in ("```\nunclosed fence\n## still inside",
                "a lone [ bracket",
                "](https://x)",
                "**unclosed bold",
                "",
                "|"):
        assert isinstance(f(bad), str)


def test_formatting_failure_falls_back_to_the_original(monkeypatch):
    """A formatting bug must never cost a send."""
    boom = types.SimpleNamespace(match=lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(whatsapp, "_FENCE", boom)
    assert f("## Heading") == "## Heading"


# ---------------------------------------------------------------- send path

class _Res:
    is_success = True


class _Client:
    """Captures the JSON body the connector would receive."""

    posted: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, path, json=None, headers=None):
        _Client.posted.append((path, json))
        return _Res()


def _wire(monkeypatch):
    _Client.posted = []
    recorded = []

    async def fake_record_self(phone, text):
        recorded.append((phone, text))

    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(whatsapp, "_record_self", fake_record_self)

    async def same(target):          # Quick 017: no LID map in these tests
        return target
    monkeypatch.setattr(whatsapp, "_recipient", same)
    monkeypatch.setattr(whatsapp, "get_settings",
                        lambda: types.SimpleNamespace(wa_api_base="http://c", wa_api_key="k"))
    return recorded


async def test_send_message_normalizes_before_recording_self(monkeypatch):
    """THE test: what is sent and what is recorded must be the same string, or
    the echo stops matching and the bot triages its own message."""
    recorded = _wire(monkeypatch)
    ok = await whatsapp.send_message("911234567890", "## Profile\n**CIN:** U74999")
    assert ok
    (_, body), = _Client.posted
    assert body["message"] == "*Profile*\n*CIN:* U74999"
    assert recorded == [("911234567890", body["message"])]


async def test_send_document_normalizes_its_caption(monkeypatch, tmp_path):
    _wire(monkeypatch)
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-1.4 stub")
    assert await whatsapp.send_document("911234567890", str(p), "## Q3 numbers")
    (_, body), = _Client.posted
    assert body["caption"] == "*Q3 numbers*"


# ---------------------------------------------------------------- prompt contract

def test_supervisor_prompt_no_longer_contradicts_itself():
    """It used to say 'Quote its sources in your reply' and, seven lines later,
    'no links' — so the model dropped citations entirely. The sources rule must
    now be stated as the explicit exception to the no-links rule."""
    from app.agents.graph import DEFAULT_PROMPTS

    p = DEFAULT_PROMPTS["supervisor"]
    assert "Sources:" in p
    assert "exception to the no-links rule" in p
    assert "Quote its sources" not in p
    assert "no markdown tables, no links" not in p       # the absolute ban is gone
