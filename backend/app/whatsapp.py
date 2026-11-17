"""Client for the WhatsApp connector's API — the ONLY module that talks to it."""

import contextvars
import logging
import re

import httpx

from . import appchannel
from .config import get_settings

log = logging.getLogger("whatsapp")


# ── markdown → WhatsApp ─────────────────────────────────────────────────
# WhatsApp renders *bold*, _italic_, ~strike~ and ```mono``` — and nothing else.
# It has no headings, no horizontal rules, no tables, no [text](url). Models
# trained on markdown emit all four anyway, and the user then reads literal
# hashes and double asterisks.
#
# This runs at the send chokepoint rather than in a prompt because twelve
# modules generate outbound text (supervisor, playbooks, triage group questions,
# docintel, sentinel, memory_pipeline, onboarding, actionsvc, ...), each with its
# own prompt. Prompts also drift; this does not.
#
# The `messages` row keeps the model's ORIGINAL text — worker.py writes it
# separately from the send — so the dashboard transcript stays rich while the
# delivered message is normalized. That divergence is deliberate.

_FENCE = re.compile(r"^\s*```")
_LINK = re.compile(r"\[([^\]\n]*)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s{0,3}(?:(?:-|\*|_)\s*){3,}$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_BOLD_U = re.compile(r"__(.+?)__")
_BULLET = re.compile(r"^(\s*)[*+]\s+")
_BLANKS = re.compile(r"\n{3,}")


def _emphasis(line: str) -> str:
    """**x** / __x__ -> *x*. A single asterisk is left alone: it is already
    WhatsApp's own syntax, which is what makes this function idempotent."""
    return _BOLD_U.sub(r"*\1*", _BOLD.sub(r"*\1*", line))


def _table_row(line: str) -> str | None:
    """A pipe-table row -> 'cell — cell'. Returns '' for a separator row (which
    is dropped), or None when the line is not a table row at all."""
    s = line.strip()
    if len(s) < 3 or "|" not in s or not (s.startswith("|") or s.endswith("|")):
        return None
    cells = [c.strip() for c in s.strip("|").split("|")]
    if all(c and set(c) <= set("-: ") for c in cells):
        return ""                                   # |---|:--:| separator
    return " — ".join(c for c in cells if c)


def for_whatsapp(text: str) -> str:
    """Normalize model-written markdown into what WhatsApp actually renders.

    Idempotent, and a no-op on text carrying no markdown — most outbound
    messages (reminders, escalations, progress pings) are already plain. Never
    raises: this sits on the outbound path, so a formatting bug must not cost a
    send. Content inside ``` fences passes through untouched, because that fence
    is real WhatsApp syntax.
    """
    try:
        out, in_fence = [], False
        for line in text.split("\n"):
            if _FENCE.match(line):
                in_fence = not in_fence
                out.append(line)
                continue
            if in_fence:
                out.append(line)
                continue
            if _RULE.match(line):
                continue                            # ---, ***, ___
            row = _table_row(line)
            if row is not None:
                if row:
                    out.append(row)
                continue
            line = _LINK.sub(r"\1 (\2)", line)
            head = _HEADING.match(line)
            if head:
                # Strip emphasis the heading already carried, or "## **X**"
                # comes back out as "**X**" — the very thing being removed.
                inner = _emphasis(head.group(1)).strip().strip("*").strip()
                if inner:
                    out.append(f"*{inner}*")
                continue
            out.append(_BULLET.sub(r"\1- ", _emphasis(line)))
        return _BLANKS.sub("\n\n", "\n".join(out)).strip("\n")
    except Exception:
        log.debug("whatsapp formatting failed — sending as-is", exc_info=True)
        return text


async def _recipient(target: str) -> str:
    """Quick 017: the address a send to `target` (a thread id or a number)
    goes to. A sender known only by a private ID (LID) threads as "<lid>@lid";
    a thread created before Quick 017 may be the bare LID. Either goes to the
    phone when the LID map has it, else to "<lid>@lid" (wasenders.recipient_for);
    the connector never turns a LID into "<lid>@s.whatsapp.net" either."""
    try:
        from . import wasenders
        return await wasenders.recipient_for(target)
    except Exception:
        log.warning("recipient lookup failed; sending as given", exc_info=True)
        return target


# ── Quick 021: a reply quotes the message it answers ──
# The worker sets the reply context for one user run (the triggering message's
# WhatsApp id, its chat and sender for groups, its text); every send to THAT thread
# during the run (the answer, progress pings, files, the failure notice) carries it
# as `replyTo` and the connector sends a quoted reply. Proactive senders (sentinel,
# triage, playbooks, digests, approvals) run in other asyncio tasks without it, and
# a send to any other recipient mid-run (send_message_to_person) never matches the
# thread, so neither is ever quoted. A contextvar rather than a parameter: twelve
# modules call send_message and only the worker knows which message a run answers.
_reply_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "wa_reply_to", default=None)
QUOTE_TEXT_CHARS = 300
_ANCHOR_PREFIX_RE = re.compile(r'^\[replying to [^\n]*?: "[^\n]*?"\] ')
_DOC_PREFIX_RE = re.compile(r"^\[document received: ([^\]\n]*)\]\s*")
_DOC_INSTRUCTION = "Extract this document and propose the data entry."


def set_reply_context(ctx: dict | None):
    """Set this task's reply target; returns the token for reset_reply_context."""
    return _reply_ctx.set(ctx or None)


def reset_reply_context(token) -> None:
    try:
        _reply_ctx.reset(token)
    except Exception:  # a token from another context: just clear it
        _reply_ctx.set(None)


def quote_text(content) -> str:
    """The stored user message as the person wrote it, for a quote the connector
    has to rebuild: the gateway's own prefixes (the reply-anchoring note, the
    document marker and its synthetic instruction) come off; capped."""
    text = _ANCHOR_PREFIX_RE.sub("", str(content or ""), count=1)
    doc = _DOC_PREFIX_RE.match(text)
    if doc:
        rest = text[doc.end():].strip()
        text = rest if rest and rest != _DOC_INSTRUCTION else doc.group(1).strip()
    text = text.strip()
    return text if len(text) <= QUOTE_TEXT_CHARS else text[:QUOTE_TEXT_CHARS] + "…"


async def reply_target(pool, message_id, thread_id) -> dict | None:
    """What a run answering messages.id = message_id quotes on WhatsApp, or None
    (an app chat, a message with no WhatsApp id such as an upload or a console
    test, another thread's message). Never raises: a quote is a nicety."""
    try:
        if not message_id or not thread_id or appchannel.is_app_thread(thread_id):
            return None
        row = await pool.fetchrow(
            "SELECT thread_id, content, wa_message_id FROM messages WHERE id = $1", message_id)
        if not row or not row["wa_message_id"] or row["thread_id"] != thread_id:
            return None
        ctx = {"thread_id": thread_id, "id": str(row["wa_message_id"]),
               "text": quote_text(row["content"])}
        if thread_id.endswith("@g.us"):
            ctx["chat"] = thread_id
            sender = await pool.fetchval(
                """SELECT actor_raw FROM events WHERE source = 'whatsapp'
                     AND provenance = 'external' AND external_id = $1
                   ORDER BY id LIMIT 1""", row["wa_message_id"])
            if sender:
                ctx["participant"] = str(sender)
        return ctx
    except Exception:
        log.debug("reply target lookup failed (sent unquoted)", exc_info=True)
        return None


def _reply_to(phone) -> dict | None:
    """The connector's replyTo for a send to `phone`: only to the run's own thread."""
    ctx = _reply_ctx.get()
    if not ctx or not phone or str(phone) != ctx.get("thread_id"):
        return None
    return {k: v for k, v in ctx.items() if k != "thread_id" and v}
# ── End Quick 021 block ──


async def send_message(phone: str, message: str) -> bool:
    if appchannel.is_app_thread(phone):  # Phase 8: in-app thread, no WhatsApp leg and no self-echo record
        return False
    # Normalize BEFORE both the POST and _record_self: connectors._is_self_echo
    # matches on exact content, so recording the pre-normalized string while
    # sending the normalized one would stop the Baileys echo from matching, and
    # the assistant's own message would enter triage as an external event.
    message = for_whatsapp(message)
    settings = get_settings()
    if not settings.wa_api_base:
        # Connector not configured yet (Setup page). Don't crash the caller —
        # the reply is still recorded in the thread and visible on the dashboard.
        log.warning("WhatsApp connector not configured — reply to %s not sent", phone)
        return False
    to = await _recipient(phone)
    body = {"phone": to, "message": message}
    reply_to = _reply_to(phone)  # Quick 021
    if reply_to:
        body["replyTo"] = reply_to
    async with httpx.AsyncClient(base_url=settings.wa_api_base, timeout=30) as client:
        res = await client.post(
            "/api/message",
            json=body,
            headers={"x-api-key": settings.wa_api_key},
        )
    if res.is_success:
        await _record_self(phone, message)
    return res.is_success


async def _record_self(phone: str, text: str) -> None:
    """Record every outbound message as a self-provenance event so its Baileys
    echo is recognized and excluded from triage. Best-effort; never blocks a send.
    Lazy import keeps this low-level client free of a connector import cycle."""
    try:
        from .connectors.whatsapp_events import record_self
        await record_self(phone, text)
    except Exception:
        pass


LID_LOOKUP_TIMEOUT_S = 5.0


async def lookup_lids(phones: list[str]) -> list[dict] | None:
    """Quick 017: the private IDs (LIDs) of up to 50 phone numbers, from the
    connector's POST /api/lid-lookup (Baileys' documented one-way PN -> LID
    lookup). Returns [{"phone", "lid"}] or None on ANY failure: fail-soft, the
    caller treats None as "nothing learned"."""
    settings = get_settings()
    phones = [p for p in (phones or []) if p][:50]
    if not settings.wa_api_base or not phones:
        return None
    try:
        async with httpx.AsyncClient(base_url=settings.wa_api_base,
                                     timeout=LID_LOOKUP_TIMEOUT_S) as client:
            res = await client.post("/api/lid-lookup", json={"phones": phones},
                                    headers={"x-api-key": settings.wa_api_key})
        if not res.is_success:
            log.warning("connector LID lookup answered %s", res.status_code)
            return None
        pairs = (res.json() or {}).get("pairs")
    except Exception as e:
        log.warning("connector LID lookup failed: %s", type(e).__name__)
        return None
    if not isinstance(pairs, list):
        return None
    return [p for p in pairs if isinstance(p, dict) and p.get("phone") and p.get("lid")]


async def fetch_group_members(jid: str) -> dict | None:
    """Roster of a group from the connector's GET /api/group-members.

    Returns {"subject": str|None, "members": [{"number","admin","raw_id"}...]}
    or None on ANY failure (unconfigured, connection error, non-2xx, bad
    payload). Fail-soft: a missing roster must never break message handling —
    and the caller writes NOTHING on None, so a flaky connector can't wipe a
    previously-good roster. Returns a dict (not just the list) because the
    subject rides along and sync_members refreshes chats.subject with it.
    """
    settings = get_settings()
    if not settings.wa_api_base:
        log.warning("WhatsApp connector not configured — cannot fetch members of %s", jid)
        return None
    try:
        async with httpx.AsyncClient(base_url=settings.wa_api_base, timeout=30) as client:
            res = await client.get(
                "/api/group-members", params={"jid": jid},
                headers={"x-api-key": settings.wa_api_key},
            )
        if not res.is_success:
            log.warning("group-members %s -> HTTP %s", jid, res.status_code)
            return None
        body = res.json()
        members = body.get("members")
        if not body.get("success") or not isinstance(members, list):
            return None
        return {"subject": body.get("subject"), "members": members}
    except Exception:
        log.warning("group-members fetch failed for %s", jid, exc_info=True)
        return None


async def fetch_qr() -> dict | None:
    """Pairing state from the connector's GET /api/qr-data.

    Returns {"connected": bool, "qr": dataURL|None, "user": {...}|None} or
    None on any failure — the dashboard page renders "connector unreachable"
    from None, which is itself useful state for the operator.
    """
    settings = get_settings()
    if not settings.wa_api_base:
        return None
    try:
        async with httpx.AsyncClient(base_url=settings.wa_api_base, timeout=15) as client:
            res = await client.get("/api/qr-data",
                                   headers={"x-api-key": settings.wa_api_key})
        if not res.is_success:
            return None
        body = res.json()
        if not body.get("success"):
            return None
        return {"connected": bool(body.get("connected")),
                "qr": body.get("qr"), "user": body.get("user")}
    except Exception:
        log.debug("qr-data fetch failed", exc_info=True)
        return None


MIMETYPES = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".png": "image/png",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


async def send_document(phone: str, file_path: str, caption: str = "") -> bool:
    """Send a file over WhatsApp via the connector's /api/send-document."""
    if appchannel.is_app_thread(phone):  # Phase 8: in-app thread, no WhatsApp leg and no self-echo record
        return False
    import base64
    from pathlib import Path

    settings = get_settings()
    if not settings.wa_api_base:
        log.warning("WhatsApp connector not configured — document to %s not sent", phone)
        return False
    p = Path(file_path)
    if not p.is_file():
        return False
    payload = {
        "phone": await _recipient(phone),
        "filename": p.name,
        "mimetype": MIMETYPES.get(p.suffix.lower(), "application/octet-stream"),
        "data": base64.b64encode(p.read_bytes()).decode(),
        "caption": for_whatsapp(caption),
    }
    reply_to = _reply_to(phone)  # Quick 021: a file that answers a message quotes it
    if reply_to:
        payload["replyTo"] = reply_to
    async with httpx.AsyncClient(base_url=settings.wa_api_base, timeout=120) as client:
        res = await client.post(
            "/api/send-document", json=payload,
            headers={"x-api-key": settings.wa_api_key},
        )
    return res.is_success
