"""Phase 8: in-app chat channel helpers.

The operator UI talks to the agent through threads named ``ui:<operator>``.
Pure helpers only; no imports beyond ``re`` so whatsapp.py and worker.py can
depend on this module without cycles.
"""
import re

APP_PREFIX = "ui:"
DEFAULT_OPERATOR = "owner"
# Letters, '-' and '_' only. No digits: worker code treats digit strings as
# phone numbers, so a slug with digits could be mistaken for a sender.
OPERATOR_RE = re.compile(r"^[a-z][a-z_-]{0,31}$")


def is_app_thread(thread_id) -> bool:
    return isinstance(thread_id, str) and thread_id.startswith(APP_PREFIX)


RESERVED_THREAD_IDS = frozenset({"dashboard", "inbox"})


def is_reserved_thread(value) -> bool:
    """True for thread ids only the gateway itself may write (ui:*, dashboard,
    inbox). Anything arriving from the connector with one of them would borrow
    operator trust (review WR-05)."""
    v = str(value or "").strip().lower()
    return v.startswith(APP_PREFIX) or v in RESERVED_THREAD_IDS


def thread_for(operator: str | None) -> str:
    op = str(operator or "").strip().lower() or DEFAULT_OPERATOR
    if not OPERATOR_RE.match(op):
        raise ValueError("operator must be 1-32 lowercase letters, '-' or '_'")
    return APP_PREFIX + op


# ── Quick 003: several chats per operator ──
# The main chat stays ui:<operator> (nothing moves); every other chat is
# ui:<operator>:<key>, the key being lowercase letters only for the same
# no-digits reason as OPERATOR_RE. ':' cannot appear in an operator, so one
# operator's chats never share a prefix with another operator's.
CHAT_KEY_LEN = 10
CHAT_RE = re.compile(r"^ui:([a-z][a-z_-]{0,31})(?::([a-z]{%d}))?$" % CHAT_KEY_LEN)


def parse_chat(thread_id) -> tuple[str, str | None] | None:
    """(operator, key) for an Ask chat thread id, key None for the main chat;
    None for anything else."""
    m = CHAT_RE.match(thread_id) if isinstance(thread_id, str) else None
    return (m.group(1), m.group(2)) if m else None


def new_chat_thread(operator: str | None) -> str:
    import secrets
    import string
    key = "".join(secrets.choice(string.ascii_lowercase) for _ in range(CHAT_KEY_LEN))
    return f"{thread_for(operator)}:{key}"


def main_thread_of(thread_id) -> str | None:
    """The operator's main chat for any of that operator's chats, else None."""
    parsed = parse_chat(thread_id)
    return APP_PREFIX + parsed[0] if parsed else None


def same_owner(a, b) -> bool:
    """True when a and b are the same conversation, or two Ask chats of one
    operator: an owner's chats are one person, so a watch or insight from one
    chat is in scope in the others. Other threads compare exactly."""
    if not a or not b:
        return False
    if a == b:
        return True
    ma = main_thread_of(a)
    return ma is not None and ma == main_thread_of(b)
# ── End Quick 003 ──


async def deliver(thread_id: str, message: str) -> bool:
    """Write an out-of-band message (watch alert) into an in-app thread.

    The worker's reply path inserts its own assistant row, so only senders that
    bypass the worker (sentinel alerts) use this. Never enters triage, so no
    self-echo record is needed.
    """
    from .db import get_pool  # lazy: keeps this module free of import cycles
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO threads (thread_id, phone) VALUES ($1, $1) ON CONFLICT DO NOTHING",
        thread_id)
    await pool.execute(
        "INSERT INTO messages (thread_id, role, content) VALUES ($1, 'assistant', $2)",
        thread_id, message)
    return True
