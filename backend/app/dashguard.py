"""Quick 036: per-run guard rails for dashboard work, enforced in tool code.

One RunGuard per agent run (one graph.run: a chat turn, its grounding re-ask, a watch
investigation), held in a ContextVar that graph.run sets and resets. Tools mutate it
in place (the ctx_skills_loaded pattern), so the caps and the preview handle end with
the run: nothing is shared between runs (no cross-run preview reuse) and nothing
survives a restart (a requeued run starts fresh). Outside graph.run (tests, scripts)
there is no guard: nothing is counted and no preview handle is issued.

Each check-and-reserve below has no await inside it, so a cap holds inside one
parallel tool batch (one event loop), as in livebudget.check.
"""
import copy
import secrets
from contextvars import ContextVar
from dataclasses import dataclass

from .config import get_settings

DELEGATION_REFUSAL = (
    "REFUSED: the dashboard specialist already ran {n} times in this request, the limit "
    "per request. Do not delegate again: report its last result to the owner (quote any "
    "action #N it returned) and, if the dashboard is still missing, suggest a smaller one "
    "in a new message.")
PREVIEW_REFUSAL = (
    "PREVIEW LIMIT REACHED: this run has used its {n} preview rounds; do not preview "
    "again. Save now with save_dashboard_version{hint}, or stop and report what is still "
    "wrong.")
_HINT_ID = (" (preview_id \"{pid}\" saves the last spec that previewed without errors, "
            "unchanged; send a spec only if you changed it after that preview, and save "
            "checks it the same way)")
_HINT_SPEC = " with the corrected spec (save checks it the same way)"
SAVED_KEYS = ("stored", "queries", "title")


@dataclass
class RunGuard:
    dashboard_delegations: int = 0
    preview_rounds: int = 0
    preview_id: str | None = None
    preview: dict | None = None   # stored, queries, title of the latest clean preview


ctx_guard: ContextVar[RunGuard | None] = ContextVar("ctx_dashguard", default=None)


def begin():
    """Called by graph.run: a fresh guard for this run. Returns the reset token."""
    return ctx_guard.set(RunGuard())


def end(token) -> None:
    ctx_guard.reset(token)


def _cap(name: str, default: int) -> int:
    try:
        return max(1, int(getattr(get_settings(), name, default)))
    except (TypeError, ValueError):
        return default


def take_delegation() -> str | None:
    """Reserve one dashboard delegation; the fixed refusal once the cap is spent."""
    g = ctx_guard.get()
    if g is None:
        return None
    cap = _cap("dashboard_delegations_per_run", 2)
    if g.dashboard_delegations >= cap:
        return DELEGATION_REFUSAL.format(n=cap)
    g.dashboard_delegations += 1
    return None


def take_preview_round() -> tuple[str | None, int | None]:
    """Reserve one preview round. Returns (refusal | None, rounds left after this one;
    None outside a run)."""
    g = ctx_guard.get()
    if g is None:
        return None, None
    cap = _cap("dashboard_preview_rounds", 3)
    if g.preview_rounds >= cap:
        hint = _HINT_ID.format(pid=g.preview_id) if g.preview_id else _HINT_SPEC
        return PREVIEW_REFUSAL.format(n=cap, hint=hint), 0
    g.preview_rounds += 1
    return None, cap - g.preview_rounds


def remember_preview(prepared: dict) -> str | None:
    """Keep a clean preview's prepared spec (a deep copy) as this run's latest; returns
    its handle, or None outside a run."""
    g = ctx_guard.get()
    if g is None:
        return None
    g.preview = copy.deepcopy({k: prepared[k] for k in SAVED_KEYS})
    if prepared.get("renderer"):  # Phase 22: v2 drafts save on ic-render-2.0.0
        g.preview["renderer"] = prepared["renderer"]
    g.preview_id = "pv_" + secrets.token_hex(4)
    return g.preview_id


def latest_preview_id() -> str | None:
    g = ctx_guard.get()
    return g.preview_id if g is not None else None


def previewed(preview_id) -> dict | None:
    """A deep copy of the prepared spec behind `preview_id` when it is this run's latest
    clean preview; None otherwise (unknown, older, another run's, or no run)."""
    g = ctx_guard.get()
    if g is None or g.preview is None or not preview_id or preview_id != g.preview_id:
        return None
    return copy.deepcopy(g.preview)
