"""DEPRECATED — renamed to db_poller.py. This shim re-exports for backward
compatibility; safe to delete (git rm app/connectors/erp_poller.py)."""

from .db_poller import DbPoller as ErpPoller  # noqa: F401
from .db_poller import _row_fingerprint  # noqa: F401
