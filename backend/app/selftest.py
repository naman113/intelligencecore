"""Connectivity self-test:  agentctl selftest   (or: uv run python -m app.selftest)

Deliberately thin. Every actual probe lives in configservice.test_group(), which
is the same code path the dashboard's Test buttons use — an operator and the UI
must never be able to disagree about whether a connection works.

It also loads the DB-backed overrides first. On a client VM almost nothing is in
.env: install.sh writes only the memory DB URL and the generated secrets, and
everything else is entered on the Setup page. Reading .env alone reported every
group as broken on a perfectly healthy install.
"""

import asyncio
import sys

from . import configservice, secretbox


async def main() -> int:
    secretbox.install_log_redaction()    # Quick 029 F7: nothing below prints a secret
    secretbox.refresh_known_secrets()    # .env secrets; DB-saved ones after load_overrides
    print("── Loading configuration ──────────────────────────────")
    try:
        n = await configservice.load_overrides()
        secretbox.refresh_known_secrets()    # DB-saved secrets are known only now
        print(f"  ✅ {n} dashboard-saved value(s) applied (plus .env and defaults)")
    except Exception as e:
        print(f"  ⚠️  could not read saved config ({type(e).__name__}: {secretbox.redact(str(e))})")
        print("      Falling back to .env only — is the memory DB up?")
        print("      Try: docker compose up -d")

    print("\n── Connections ────────────────────────────────────────")
    status = await configservice.status()
    required_failures = 0
    optional_notes = 0

    for group, title in configservice.GROUPS.items():
        optional = configservice.is_optional(group)  # Phase 10
        if status[group]["state"] == "not_configured":
            if optional:
                print(f"  ⚪ {title} — not configured (optional)")
                optional_notes += 1
            else:
                print(f"  ❌ {title} — not configured")
                required_failures += 1
            continue

        result = await configservice.test_group(group)
        if result["ok"]:
            print(f"  ✅ {title} — {secretbox.redact(str(result['detail']))}")
        elif optional:
            print(f"  ⚪ {title} — {secretbox.redact(str(result['detail']))} (optional)")
            optional_notes += 1
        else:
            print(f"  ❌ {title} — {secretbox.redact(str(result['detail']))}")
            required_failures += 1

    print()
    if required_failures:
        print(f"{required_failures} required connection(s) failing — "
              f"fix them on the dashboard Setup page.")
    else:
        print("All required connections OK." + (
            f" ({optional_notes} optional group(s) not configured — that is fine.)"
            if optional_notes else ""))
    # Optional groups never fail the exit code: tracing has no keys until a
    # human creates a Langfuse project, so gating go-live on it would block
    # 100% of installs.
    return 1 if required_failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
