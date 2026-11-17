"""Phase 8: in-app chat channel. POST queues one agent task on an Ask chat;
GET reads the chat back. UI create buttons send the agent a message here
(build rule: the UI never creates things itself).

Quick 003: an operator has several chats. The main chat is ui:<operator> (the
thread every earlier phase used, so nothing moves); the others are
ui:<operator>:<key>. Chats are listed, created, renamed, hidden and marked read
here, a failed answer can be retried, and GET says what the answer in progress
is doing (runstatus) so the panel can show it. Quick 007: GET also carries
each reply's attachments, the files its run delivered with send_document
(chatfiles). Plumbing only: no agent behaviour lives in this module."""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from . import appchannel, chatfiles, db, runstatus, secretbox
from .config import get_settings

router = APIRouter(prefix="/api/ask", tags=["ask"])

MAX_MESSAGE = 4000
MAX_TITLE = 80
TITLE_CHARS = 60
MAX_CHATS = 50
DOC_PREFIX = "[document received: "
ACTIVE = ("queued", "running")
RUN_COLS = "id, status, error, message_id, created_at, started_at, ended_at"


def _clean(text) -> str:
    s = "" if text is None else str(text)
    s = "".join(c for c in s if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))
    return s.strip()


def _thread(operator) -> str:
    try:
        return appchannel.thread_for(operator)
    except ValueError as e:
        raise HTTPException(422, str(e))


def _chat(chat, operator=None) -> str:
    """Thread id for a request: the given chat id, else the operator's main chat."""
    if chat is None or str(chat).strip() == "":
        return _thread(operator)
    c = str(chat).strip()
    if appchannel.parse_chat(c) is None:
        raise HTTPException(422, "chat must be an Ask chat id such as ui:owner")
    return c


def _is_main(thread_id: str) -> bool:
    parsed = appchannel.parse_chat(thread_id)
    return bool(parsed) and parsed[1] is None


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def title_from(first) -> str | None:
    """A chat title from its first message: one line, at most TITLE_CHARS."""
    text = str(first or "").strip()
    if text.startswith(DOC_PREFIX):
        text = "Document: " + text[len(DOC_PREFIX):].split("]", 1)[0].strip()
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS - 1].rstrip() + "…"


async def _require_live(conn, thread_id: str, lock: str = "") -> None:
    """404 for a deleted chat, or for an extra chat that was never created. The
    main chat always exists (its row is written on first use)."""
    row = await conn.fetchrow(
        f"SELECT deleted_at FROM threads WHERE thread_id = $1 {lock}", thread_id)
    if row is not None and row["deleted_at"] is not None:
        raise HTTPException(404, "that chat was deleted")
    if row is None and not _is_main(thread_id):
        raise HTTPException(404, "chat not found")


# ── what the chat's answer is doing ─────────────────────────────────────

async def _run_state(conn, thread_id: str) -> dict | None:
    """The answer this chat is on: the running one, else the oldest queued one,
    else the latest run (so a failure stays visible until the next message)."""
    rows = await conn.fetch(
        f"""SELECT {RUN_COLS} FROM task_runs WHERE thread_id = $1 AND status = ANY($2::text[])
            ORDER BY (status = 'running') DESC, id LIMIT 20""", thread_id, list(ACTIVE))
    if rows:
        run = dict(rows[0])
        run["queued_behind"] = len(rows) - 1
    else:
        last = await conn.fetchrow(
            f"SELECT {RUN_COLS} FROM task_runs WHERE thread_id = $1 ORDER BY id DESC LIMIT 1",
            thread_id)
        if last is None:
            return None
        run = dict(last)
        run["queued_behind"] = 0
    run["step"] = None
    if run["status"] == "running":
        run["step"] = runstatus.step(run["id"]) or runstatus.FIRST_STEP
    elif run["status"] == "queued":
        busy = int(await conn.fetchval(
            "SELECT count(*) FROM task_runs WHERE status = 'running'") or 0)
        run["step"] = (f"Waiting for a free worker ({busy} answers running)"
                       if busy >= max(1, get_settings().worker_concurrency) else "Starting")
    for k in ("created_at", "started_at", "ended_at"):
        run[k] = _iso(run.get(k))
    return run


async def _waiting_approval(conn, run: dict | None) -> int:
    """Proposals the chat's last finished answer left for the owner to decide."""
    if not run or run["status"] != "done":
        return 0
    return int(await conn.fetchval(
        "SELECT count(*) FROM actions WHERE task_run_id = $1 AND status = 'proposed'",
        run["id"]) or 0)


# ── messages ────────────────────────────────────────────────────────────

@router.post("")
async def ask(body: dict | None = None):
    body = body or {}
    text = _clean(body.get("message"))
    # Phase 15 (SRC-02, gap 1): pasted secrets never reach messages or the agent
    text = secretbox.scrub_pasted_secrets(text)[0]
    if not text:
        raise HTTPException(422, "message is required")
    if len(text) > MAX_MESSAGE:
        raise HTTPException(422, f"message is longer than {MAX_MESSAGE} characters")
    thread_id = _chat(body.get("chat"), body.get("operator"))
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if _is_main(thread_id):
                await conn.execute(
                    "INSERT INTO threads (thread_id, phone, last_read_id) VALUES ($1, $1, 0) "
                    "ON CONFLICT DO NOTHING", thread_id)
            else:
                await _require_live(conn, thread_id, "FOR SHARE")
            mid = await conn.fetchval(
                "INSERT INTO messages (thread_id, role, content) "
                "VALUES ($1, 'user', $2) RETURNING id", thread_id, text)
            rid = await conn.fetchval(
                "INSERT INTO task_runs (thread_id, message_id) "
                "VALUES ($1, $2) RETURNING id", thread_id, mid)
    return {"thread_id": thread_id, "message_id": mid, "run_id": rid}


@router.get("")
async def thread(operator: str = "owner", limit: int = 50, chat: str | None = None):
    thread_id = _chat(chat, operator)
    limit = max(1, min(int(limit), 200))
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await _require_live(conn, thread_id)
        rows = await conn.fetch(
            "SELECT id, role, content, created_at, reply_to FROM messages "  # Quick 021: reply_to
            "WHERE thread_id = $1 ORDER BY id DESC LIMIT $2", thread_id, limit)
        messages = [dict(r) for r in reversed(rows)]
        # Quick 007: files this chat's runs delivered, under the reply they belong to.
        files = await chatfiles.for_thread(conn, thread_id, messages)
        run = await _run_state(conn, thread_id)
        waiting = await _waiting_approval(conn, run)
    for m in messages:
        if m["role"] == "assistant":
            m["attachments"] = files.get(m["id"], [])
    pending = bool(run and run["status"] in ACTIVE)
    return {
        "thread_id": thread_id,
        "messages": messages,
        "pending": pending,
        # Phase 8 shape, kept for older clients: the latest run's outcome.
        "last_run": ({"id": run["id"], "status": run["status"], "error": run["error"]}
                     if run else None),
        "run": run,
        "waiting_approval": waiting,
        "now": _now(),
    }


@router.post("/retry")
async def retry(body: dict | None = None):
    """Queue a failed answer again: a new task run for the same user message,
    so the thread does not repeat the question. Only the chat's latest run,
    only when it failed."""
    body = body or {}
    thread_id = _chat(body.get("chat"), body.get("operator"))
    run_id = body.get("run_id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or not 0 < run_id < 2**63:
        raise HTTPException(422, "run_id must be a task run id")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_live(conn, thread_id, "FOR UPDATE")
            run = await conn.fetchrow(
                "SELECT id, thread_id, message_id, status FROM task_runs WHERE id = $1", run_id)
            if run is None or run["thread_id"] != thread_id:
                raise HTTPException(404, "no such answer in this chat")
            latest = await conn.fetchval(
                "SELECT max(id) FROM task_runs WHERE thread_id = $1", thread_id)
            if run["status"] != "failed" or latest != run["id"] or run["message_id"] is None:
                raise HTTPException(409, "only the latest failed answer can be retried")
            new_id = await conn.fetchval(
                "INSERT INTO task_runs (thread_id, message_id) VALUES ($1, $2) RETURNING id",
                thread_id, run["message_id"])
    return {"thread_id": thread_id, "run_id": new_id, "retry_of": run_id}


# ── chats ───────────────────────────────────────────────────────────────

_LIST_SQL = """
SELECT t.thread_id, t.title, t.created_at, t.last_read_id,
       (SELECT m.content FROM messages m WHERE m.thread_id = t.thread_id AND m.role = 'user'
         ORDER BY m.id LIMIT 1) AS first_message,
       lm.last_at, lm.last_assistant_id,
       r.id AS run_id, r.status AS run_status, r.error AS run_error
  FROM threads t
  LEFT JOIN LATERAL (SELECT max(m.created_at) AS last_at,
                            max(m.id) FILTER (WHERE m.role = 'assistant') AS last_assistant_id
                       FROM messages m WHERE m.thread_id = t.thread_id) lm ON true
  LEFT JOIN LATERAL (SELECT x.id, x.status, x.error FROM task_runs x
                      WHERE x.thread_id = t.thread_id
                      ORDER BY (x.status = 'running') DESC, (x.status = 'queued') DESC, x.id DESC
                      LIMIT 1) r ON true
 WHERE (t.thread_id = $1::text OR left(t.thread_id, length($2::text)) = $2::text)
   AND t.deleted_at IS NULL
 ORDER BY COALESCE(lm.last_at, t.created_at) DESC
 LIMIT $3"""


def chat_item(row: dict, main: str) -> dict:
    """One chat for the list. Unread only once the chat has a read mark: chats
    from before Quick 003 start with nothing unread."""
    tid = row["thread_id"]
    status = row.get("run_status")
    last_a, read = row.get("last_assistant_id"), row.get("last_read_id")
    return {
        "id": tid,
        "is_main": tid == main,
        "title": (row.get("title") or title_from(row.get("first_message"))
                  or ("Main chat" if tid == main else "New chat")),
        "named": bool(row.get("title")),
        "created_at": _iso(row.get("created_at")),
        "last_activity_at": _iso(row.get("last_at") or row.get("created_at")),
        "last_assistant_id": last_a,
        "unread": bool(last_a is not None and read is not None and last_a > read),
        "run": ({"id": row["run_id"], "status": status, "error": row.get("run_error"),
                 "step": (runstatus.step(row["run_id"]) or runstatus.FIRST_STEP)
                 if status == "running" else None}
                if row.get("run_id") is not None else None),
    }


@router.get("/chats")
async def list_chats(operator: str = "owner"):
    main = _thread(operator)
    pool = await db.get_pool()
    rows = await pool.fetch(_LIST_SQL, main, main + ":", MAX_CHATS)
    items = [chat_item(dict(r), main) for r in rows]
    if not any(i["is_main"] for i in items):
        items.append(chat_item({"thread_id": main}, main))
    return {"operator": main[len(appchannel.APP_PREFIX):], "chats": items, "now": _now()}


@router.post("/chats")
async def create_chat(body: dict | None = None):
    body = body or {}
    title = _clean(body.get("title"))[:MAX_TITLE] or None
    thread_id = _new_thread(body.get("operator"))
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "INSERT INTO threads (thread_id, phone, title, last_read_id) VALUES ($1, $1, $2, 0) "
        "RETURNING thread_id, title, created_at, last_read_id", thread_id, title)
    return chat_item(dict(row), appchannel.main_thread_of(thread_id))


def _new_thread(operator) -> str:
    try:
        return appchannel.new_chat_thread(operator)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.patch("/chats/{chat}")
async def rename_chat(chat: str, body: dict | None = None):
    """Rename a chat; an empty title goes back to the first-message title."""
    thread_id = _chat(chat)
    raw = (body or {}).get("title")
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(422, "title must be text")
    title = " ".join(_clean(raw).split())
    if len(title) > MAX_TITLE:
        raise HTTPException(422, f"title is longer than {MAX_TITLE} characters")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if _is_main(thread_id):
                await conn.execute(
                    "INSERT INTO threads (thread_id, phone, last_read_id) VALUES ($1, $1, 0) "
                    "ON CONFLICT DO NOTHING", thread_id)
            await _require_live(conn, thread_id, "FOR UPDATE")
            await conn.execute("UPDATE threads SET title = $2 WHERE thread_id = $1",
                               thread_id, title or None)
    return {"id": thread_id, "title": title or None}


@router.delete("/chats/{chat}")
async def delete_chat(chat: str):
    """Hide a chat. Its rows stay (runs, approvals and learnings point at them);
    its watches and still-proposed watch actions move to the main chat so their
    alerts keep reaching the owner. The main chat stays."""
    thread_id = _chat(chat)
    if _is_main(thread_id):
        raise HTTPException(409, "the main chat cannot be deleted (uploads and alerts land "
                                 "there); rename it instead")
    main = appchannel.main_thread_of(thread_id)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_live(conn, thread_id, "FOR UPDATE")
            if await conn.fetchval(
                    "SELECT 1 FROM task_runs WHERE thread_id = $1 AND status = ANY($2::text[]) "
                    "LIMIT 1", thread_id, list(ACTIVE)):
                raise HTTPException(409, "an answer is still running in this chat; delete it "
                                         "once it has finished")
            await conn.execute("UPDATE threads SET deleted_at = now() WHERE thread_id = $1",
                               thread_id)
            await conn.execute(
                "INSERT INTO threads (thread_id, phone, last_read_id) VALUES ($1, $1, 0) "
                "ON CONFLICT DO NOTHING", main)
            moved = await conn.fetchval(
                """WITH m AS (UPDATE watches SET thread_id = $2
                               WHERE thread_id = $1 AND status <> 'done' RETURNING 1)
                   SELECT count(*) FROM m""", thread_id, main)
            await conn.execute(
                """UPDATE actions SET payload = jsonb_set(payload, '{thread_id}', to_jsonb($2::text))
                    WHERE status = 'proposed' AND kind = 'create_watch'
                      AND payload->>'thread_id' = $1""", thread_id, main)
    return {"deleted": thread_id, "watches_moved": int(moved or 0), "main": main}


@router.post("/chats/{chat}/read")
async def mark_read(chat: str, body: dict | None = None):
    """The owner has seen this chat up to message `upto` (never moves back)."""
    thread_id = _chat(chat)
    upto = (body or {}).get("upto")
    if isinstance(upto, bool) or not isinstance(upto, int) or not 0 <= upto < 2**63:
        raise HTTPException(422, "upto must be a message id")
    pool = await db.get_pool()
    if _is_main(thread_id):
        await pool.execute(
            "INSERT INTO threads (thread_id, phone, last_read_id) VALUES ($1, $1, 0) "
            "ON CONFLICT DO NOTHING", thread_id)
    row = await pool.fetchrow(
        """UPDATE threads SET last_read_id = GREATEST(COALESCE(last_read_id, 0), $2)
            WHERE thread_id = $1 AND deleted_at IS NULL RETURNING last_read_id""",
        thread_id, upto)
    if row is None:
        raise HTTPException(404, "chat not found")
    return {"id": thread_id, "last_read_id": row["last_read_id"]}
