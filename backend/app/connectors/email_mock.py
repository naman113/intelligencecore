"""Email connector — mock/file implementation.

The real email provider is undecided (plan §1.7), so this is the test harness:
drop JSON files into artifacts/inbox_mock/ and each becomes an email event. This
drives the accounts-team scenario end-to-end without committing to IMAP/Gmail.

File shape (any *.json in the inbox):
    {"from": "priya@acme.com", "to": "ops@us.com", "subject": "March sheet",
     "body": "Attached.", "thread_id": "t-123", "date": "2026-03-20T10:00:00Z",
     "message_id": "m-1", "attachments": ["march.xlsx"]}

Processed files move to artifacts/inbox_mock/_processed/ so a restart doesn't
re-ingest them; (source, external_id) dedupe is the second line of defense.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import RawEvent

log = logging.getLogger("connectors.email")

INBOX = Path(__file__).resolve().parents[3] / "artifacts" / "inbox_mock"
PROCESSED = INBOX / "_processed"


def _parse_date(raw) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return datetime.now(timezone.utc)


class EmailMockConnector:
    name = "email_mock"

    async def poll(self) -> list[RawEvent]:
        if not INBOX.is_dir():
            return []
        out: list[RawEvent] = []
        for f in sorted(INBOX.glob("*.json")):
            try:
                data = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning("email_mock: could not parse %s; skipping", f.name)
                self._quarantine(f)
                continue
            subject = (data.get("subject") or "").strip()
            body = (data.get("body") or "").strip()
            content = f"Subject: {subject}\n\n{body}" if subject else body
            out.append(RawEvent(
                source="email",
                external_id=str(data.get("message_id") or f.stem),
                content=content or "(empty email)",
                occurred_at=_parse_date(data.get("date")),
                actor_email=data.get("from"),
                actor_raw=data.get("from"),
                thread_ref=str(data.get("thread_id") or data.get("from") or f.stem),
                meta={"to": data.get("to"), "subject": subject,
                      "attachments": data.get("attachments") or []},
            ))
            self._archive(f)
        if out:
            log.info("email_mock: ingested %d file(s)", len(out))
        return out

    @staticmethod
    def _archive(f: Path) -> None:
        try:
            PROCESSED.mkdir(parents=True, exist_ok=True)
            f.rename(PROCESSED / f.name)
        except OSError:
            log.debug("email_mock: could not archive %s", f.name, exc_info=True)

    @staticmethod
    def _quarantine(f: Path) -> None:
        try:
            bad = INBOX / "_bad"
            bad.mkdir(parents=True, exist_ok=True)
            f.rename(bad / f.name)
        except OSError:
            pass
