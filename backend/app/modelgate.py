"""Quick 039: bounded retries for a background loop's model calls.

On 2026-10-27 the Anthropic spend limit was reached and the triage loop re-sent the
same batch every 10 seconds (428 failed calls in the ledger). A ModelGate sits in
front of one loop's model calls:

  * Backoff. After a failed call no further call is made until the wait is over.
    The wait doubles with each consecutive failure, from `base` up to `cap`, and
    any success resets it (the model answers again).
  * Attempt cap. Failures are also counted per work item (a triage batch, a set of
    deliveries); at `max_attempts` the caller is told to give the item up. Giving
    up does not reset the backoff, so a long outage settles at one call per `cap`.
  * Spend-limit pause. A provider spend or credit limit refuses every call until a
    person raises it or it resets, so it pauses every call for `pause` seconds and
    is not counted against the item (nothing is wrong with the item).

Plumbing only, in memory (a restart starts clean), never raises. The caller keeps
the loop alive, records outcomes through jobruns and decides what giving up means.
"""
import re
import time
from dataclasses import dataclass

# Anthropic: HTTP 400 "You have reached your specified API usage limits. You will
# regain access on ..." and "Your credit balance is too low to access the Anthropic
# API"; OpenAI: HTTP 429 insufficient_quota. A plain rate limit (429) is not one.
SPEND_LIMIT_RE = re.compile(r"usage limits|credit balance|insufficient_quota", re.I)
MAX_ITEMS = 64

_now = time.monotonic  # tests patch this


def is_spend_limit(exc: BaseException) -> bool:
    try:
        return bool(SPEND_LIMIT_RE.search(f"{exc} {getattr(exc, 'body', '') or ''}"))
    except Exception:
        return False


def describe(exc: BaseException, limit: int = 200) -> str:
    """'BadRequestError: You have reached ...' clipped for job rows and logs."""
    msg = " ".join(str(exc).split())
    text = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Outcome:
    kind: str          # "retry" | "give_up" | "paused"
    attempts: int      # failed attempts on this item so far (0 for a pause)
    wait: float        # seconds until the next call is allowed


class ModelGate:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.failures = 0              # consecutive failures, any item
        self.retry_at = 0.0
        self.paused_until = 0.0
        self.attempts: dict = {}       # item key -> failed attempts
        self.successes = 0             # calls answered since start (or reset)

    def paused(self) -> bool:
        return _now() < self.paused_until

    def wait(self) -> float:
        """Seconds until a model call is allowed (0 = call now)."""
        return max(0.0, max(self.retry_at, self.paused_until) - _now())

    def failure(self, key, exc: BaseException, *, base: float, cap: float,
                max_attempts: int, pause: float) -> Outcome:
        now = _now()
        if is_spend_limit(exc):
            self.paused_until = now + max(1.0, float(pause))
            return Outcome("paused", 0, self.wait())
        self.failures += 1
        delay = min(float(cap), float(base) * 2 ** min(self.failures - 1, 30))
        self.retry_at = now + max(0.0, delay)
        n = self.attempts.pop(key, 0) + 1          # re-insert: newest last
        if n >= max(1, int(max_attempts)):
            return Outcome("give_up", n, self.wait())
        self.attempts[key] = n
        while len(self.attempts) > MAX_ITEMS:
            self.attempts.pop(next(iter(self.attempts)))
        return Outcome("retry", n, self.wait())

    def success(self, key=None) -> None:
        """The model answered: no wait, no pause, and `key` starts over."""
        self.failures = 0
        self.retry_at = 0.0
        self.paused_until = 0.0
        self.successes += 1
        if key is not None:
            self.attempts.pop(key, None)
