"""Quick 007: files the agent delivers into an in-app chat.

send_document on an app thread (ui:<operator>[:<key>]) attaches the file to the
run's reply instead of sending it anywhere. Nothing new is stored: an
attachment IS a send_document tool_calls row of a run on that chat plus the
file in artifacts/run_<id>/. That is also why replies from before this existed
(result "saved to the workspace as ...") show their files with no backfill.

The result strings below are written by graph.send_document and read back by
GET /api/ask, so both sides use these constants. Plumbing only: no agent
behaviour lives here, and nothing here sends anything (askapi must not import
an outbound module).
"""
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("chatfiles")

# What send_document returns on an app thread (and so what tool_calls records).
ATTACHED = "attached to the chat as a download: "
# The pre-Quick-007 app-thread result: the file was a real deliverable, only
# nothing showed it in the chat.
LEGACY_SAVED = "saved to the workspace as "
WA_SENT = "; also sent to WhatsApp"
WA_NOT_SENT = "; not sent to WhatsApp: "
WA_OFF = "WhatsApp isn't connected yet; set it up on System › WhatsApp"

_SENT_TO = re.compile(re.escape(WA_SENT) + r" \(([^)]{1,80})\)")
_HIDDEN = {"script.py", ".runner.py"}

# Extension -> (kind for the card icon, mimetype). Unknown types download as bytes.
TYPES = {
    ".xlsx": ("spreadsheet", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".xls": ("spreadsheet", "application/vnd.ms-excel"),
    ".csv": ("spreadsheet", "text/csv"),
    ".pdf": ("pdf", "application/pdf"),
    ".docx": ("document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".md": ("document", "text/markdown"),
    ".txt": ("document", "text/plain"),
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".svg": ("image", "image/svg+xml"),
}


def file_type(name: str) -> tuple[str, str]:
    return TYPES.get(Path(name).suffix.lower(), ("file", "application/octet-stream"))


def attached_result(filename: str, whatsapp_note: str = "") -> str:
    """send_document's result on an app thread. `whatsapp_note` is
    WA_SENT + ' (<to>)' or WA_NOT_SENT + reason, or '' when WhatsApp was not
    asked for."""
    if not whatsapp_note:
        tail = "."
    elif whatsapp_note.startswith(WA_SENT):
        tail = " and that it was also sent to WhatsApp."
    else:
        tail = ", and pass on why WhatsApp did not get it."
    return (f"{ATTACHED}{filename}{whatsapp_note}. Tell the user the file is attached to "
            f"your reply in the chat{tail}")


def delivered(digest: str | None) -> bool:
    """A send_document call that left the file with the user in an app chat."""
    d = digest or ""
    return d.startswith(ATTACHED) or d.startswith(LEGACY_SAVED)


def whatsapp_state(digest: str | None) -> tuple[str | None, str | None]:
    """('sent', to) / ('failed', None) / (None, None) from a result digest."""
    d = digest or ""
    m = _SENT_TO.search(d)
    if m:
        return "sent", m.group(1)
    if WA_SENT in d:
        return "sent", None
    if WA_NOT_SENT in d:
        return "failed", None
    return None, None


def artifact_path(root: Path, run_id: int, name) -> Path | None:
    """The run's file, with the artifact route's containment rule (bare name,
    resolved inside artifacts/run_<id>/); None for anything else."""
    if not isinstance(name, str) or not name or Path(name).name != name or name in _HIDDEN:
        return None
    d = (root / f"run_{int(run_id)}").resolve()
    p = (d / name).resolve()
    return p if d in p.parents else None


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):   # asyncpg returns jsonb as text
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}
    return {}


def reply_for(run: dict, assistant: list[dict]):
    """The assistant message a run's files belong under. `assistant` is the
    chat's assistant rows, oldest first.

    The reply is the last assistant row written inside the run's window (the
    worker inserts it just before closing the run; progress pings never reach an
    app thread). A failed run has no reply of its own, so its files go under the
    first assistant row after it started. A running run shows nothing yet, and a
    finished run whose reply is outside the loaded window is skipped."""
    started, ended = run.get("started_at"), run.get("ended_at")
    if started is None or ended is None:
        return None
    after = [m for m in assistant if m["created_at"] >= started]
    inside = [m for m in after if m["created_at"] <= ended]
    if inside:
        return inside[-1]["id"]
    if run.get("status") == "failed" and after:
        return after[0]["id"]
    return None


def attach(rows: list[dict], messages: list[dict], root: Path) -> dict:
    """{message id: [attachment, ...]} from send_document rows of this chat's
    runs. One card per (run, filename): a file sent twice in a run shows once."""
    assistant = [m for m in messages if m.get("role") == "assistant"]
    runs: dict[int, dict] = {}
    for r in rows:
        if not delivered(r.get("result_digest")):
            continue
        name = _args(r.get("args")).get("filename")
        rid = int(r["run_id"])
        if artifact_path(root, rid, name) is None:
            continue
        run = runs.setdefault(rid, {"run": r, "files": {}})
        wa, to = whatsapp_state(r.get("result_digest"))
        f = run["files"].setdefault(name, {"whatsapp": None, "whatsapp_to": None})
        if wa == "sent" or (wa == "failed" and f["whatsapp"] is None):
            f["whatsapp"], f["whatsapp_to"] = wa, to
    out: dict = {}
    for rid, run in runs.items():
        target = reply_for(run["run"], assistant)
        if target is None:
            continue
        for name, wa in run["files"].items():
            p = artifact_path(root, rid, name)
            size = None
            try:
                if p is not None and p.is_file():
                    size = p.stat().st_size
            except OSError:
                size = None
            kind, mime = file_type(name)
            out.setdefault(target, []).append({
                "name": name, "run_id": rid, "kind": kind, "mimetype": mime,
                "bytes": size, "available": size is not None,
                "url": f"/api/runs/{rid}/artifacts/{quote(name, safe='')}" if size is not None else None,
                "whatsapp": wa["whatsapp"], "whatsapp_to": wa["whatsapp_to"],
            })
    return out


async def for_thread(conn, thread_id: str, messages: list[dict], root: Path | None = None) -> dict:
    """Attachments for the loaded messages of one chat. Only runs whose
    thread_id is this chat are read, so one chat never lists another's files.
    Fail-soft: a lookup problem leaves the chat readable without cards."""
    try:
        if not any(m.get("role") == "assistant" for m in messages):
            return {}
        if root is None:
            from .sandbox import ARTIFACTS_ROOT
            root = ARTIFACTS_ROOT
        oldest = min(m["created_at"] for m in messages)
        rows = await conn.fetch(
            """SELECT r.id AS run_id, r.status, r.started_at, r.ended_at,
                      tc.args, tc.result_digest
                 FROM task_runs r JOIN tool_calls tc ON tc.task_run_id = r.id
                WHERE r.thread_id = $1 AND tc.tool = 'send_document'
                  AND r.ended_at >= $2
                ORDER BY r.id, tc.id""", thread_id, oldest)
        return attach([dict(r) for r in rows], messages, root)
    except Exception:
        log.warning("chat attachments lookup failed for %s", thread_id, exc_info=True)
        return {}
