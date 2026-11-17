"""Sandbox containment — the ceilings around model-written code.

run_python executes whatever the Analysis agent wrote, and the model's input
includes untrusted text from uploaded invoices, polled DB rows, and web
results. The flags below are the containment boundary, so they are asserted
rather than assumed: a silently dropped `--network none` looks exactly like a
working sandbox until something reaches the internet.
"""

from app import sandbox
from app.config import Settings


def test_defaults_are_bounded_not_unlimited():
    s = Settings()
    assert s.sandbox_memory and s.sandbox_memory != "0"
    assert s.sandbox_cpus and s.sandbox_cpus != "0"
    assert s.sandbox_pids > 0


def test_pid_ceiling_exists_because_cpu_and_memory_do_not_cover_forks():
    # A fork bomb exhausts host PIDs without touching either other ceiling, so
    # this is the one limit whose absence is invisible to memory/CPU capping.
    assert Settings().sandbox_pids <= 1024


def test_sandbox_totals_stay_within_the_app_container():
    # Worst case is worker_concurrency sandboxes at once. They are siblings on
    # the host daemon, not children, but the host still has to hold them all —
    # so the default fleet must stay well under the app's own 4G ceiling.
    s = Settings()
    assert s.sandbox_memory.lower().endswith(("m", "g"))
    mb = (float(s.sandbox_memory[:-1]) * (1024 if s.sandbox_memory.lower().endswith("g") else 1))
    assert mb * s.worker_concurrency <= 4096


def test_isolation_flags_are_present_in_source():
    # Cheap guard against a refactor quietly dropping a containment flag.
    src = open(sandbox.__file__, encoding="utf-8").read()
    for flag in ("--network", "none", "--memory", "--cpus", "--pids-limit"):
        assert flag in src, f"{flag} missing from the sandbox invocation"
