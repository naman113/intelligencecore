"""Phase 11 GOLIVE-04: static checks on agentctl backup/restore and the nightly timer."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2]


def _read(rel):
    return (APP / rel).read_text(encoding="utf-8")


CTL = _read("deploy/agentctl")
INSTALL = _read("install.sh")


def _block():
    a = CTL.index("# Phase 11: GOLIVE-04 backups\n  backup)")
    b = CTL.index("# end Phase 11 backups\n", a)
    return CTL[a:b]


def test_arms_present_and_delimited():
    blk = _block()
    assert "  backup)" in blk and "  restore)" in blk
    assert "# Phase 11: GOLIVE-04" in CTL


def test_dump_flags_and_verification_order():
    blk = _block()
    assert "pg_dump -U agent -d agent_memory -Fc" in blk
    assert ".partial" in blk
    i_list = blk.index("pg_restore --list")
    assert i_list < blk.index('mv "$BF.partial"') < blk.index("_prune_backups")
    assert ".sha256" in blk
    assert "umask 077" in blk and 'chmod 700 "$BACKUP_DIR"' in blk


def test_defaults_and_flags():
    for frag in (
        'PG_CONTAINER="${PG_CONTAINER:-agentic-postgres}"',
        'BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/backups}"',
        'BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"',
        'BACKUP_KEEP_MIN="${BACKUP_KEEP_MIN:-3}"',
        "--dir)",
        "--keep-days)",
    ):
        assert frag in CTL


def test_restore_guards():
    assert "^[a-z_][a-z0-9_]{0,62}$" in CTL
    assert "postgres|langfuse|template*" in CTL
    assert "agent_memory_restore" in CTL
    assert '"$RS_DB" == agent_memory && "$RS_FORCE" != 1' in CTL
    blk = _block()
    assert "systemctl stop agentic-api" in blk and "systemctl start agentic-api" in blk
    assert "ALTER DATABASE agent_memory RENAME TO agent_memory_before_restore_" in CTL or \
        "ALTER DATABASE agent_memory RENAME TO $RS_OLD" in blk
    assert "createdb -U agent -T template0" in blk
    assert "pg_restore -U agent --no-owner --exit-on-error" in blk


def test_never_drops_live_db():
    assert not re.search(r"DROP DATABASE", CTL, re.I)
    for line in CTL.splitlines():
        if "dropdb" in line and "agent_memory" in line:
            # only the scratch-name hint / variable form is allowed
            assert "agent_memory_restore" in line or "$RS_DB" in line or "$db" in line, line


def test_status_last_backup_line():
    assert "last backup" in CTL


def test_usage_line_lists_new_commands():
    assert "backup [--dir d] [--keep-days n]|restore <file> [--into db] [--force]" in CTL


def test_timer_units():
    assert "/etc/systemd/system/agentic-backup.service" in INSTALL
    assert "/etc/systemd/system/agentic-backup.timer" in INSTALL
    for frag in (
        "Type=oneshot",
        "User=${RUN_USER}",
        "After=docker.service",
        "ExecStart=/usr/local/bin/agentctl backup",
        "OnCalendar=*-*-* 01:30:00",
        "RandomizedDelaySec=15m",
        "Persistent=true",
        "WantedBy=timers.target",
    ):
        assert frag in INSTALL, frag
    assert INSTALL.count("systemctl enable --now agentic-backup.timer") == 1
    assert INSTALL.index("ln -sf \"$REPO_DIR/deploy/agentctl\"") < INSTALL.index("agentic-backup.timer")


def test_gitignore():
    assert re.search(r"^backups/$", _read(".gitignore"), re.M)


@pytest.mark.skipif(not shutil.which("bash"), reason="no bash")
@pytest.mark.parametrize("rel", ["deploy/agentctl", "install.sh"])
def test_bash_syntax(rel):
    r = subprocess.run(["bash", "-n", str(APP / rel)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ── Phase 14 WH-09: warehouse backups, scratch restore, enable|disable|status ──
def _wh_helpers():
    a = CTL.index("# Phase 14: WH-09 warehouse backups (helpers)")
    b = CTL.index("# end Phase 14 warehouse backups (helpers)", a)
    return CTL[a:b]


def _wh_arm():
    a = CTL.index("# Phase 14: WH-09 / D-09 warehouse enable|disable|status")
    b = CTL.index("# end Phase 14 warehouse", a)
    return CTL[a:b]


def test_p14_helpers_after_compose_helpers():
    assert CTL.index("# end Phase 14 compose helpers") < CTL.index("# Phase 14: WH-09 warehouse backups (helpers)")
    blk = _wh_helpers()
    for frag in (
        'WH_CONTAINER="${WH_CONTAINER:-agentic-warehouse}"',
        'WH_ADMIN_USER="${WH_ADMIN_USER:-wh_admin}"',
        'WH_BACKUP_KEEP_DAYS="${WH_BACKUP_KEEP_DAYS:-7}"',
        'WH_BACKUP_KEEP_MIN="${WH_BACKUP_KEEP_MIN:-3}"',
        "_wh()", "_wh_running()", "_prune_wh_backups()", "_wh_backup()", "_wh_restore()",
        "warehouse-*.dump",
    ):
        assert frag in blk, frag


def test_p14_every_pg_dump_is_allowlisted():
    lines = [ln for ln in _wh_helpers().splitlines() if "pg_dump" in ln and not ln.lstrip().startswith("#")]
    assert lines
    for ln in lines:
        assert "-n ic_meta -n model" in ln, ln
    # no warehouse pg_dump anywhere else lacks the allowlist
    for ln in CTL.splitlines():
        if "pg_dump -" in ln and "agent_memory" not in ln and not ln.lstrip().startswith("#"):
            assert "-n ic_meta" in ln, ln


def test_p14_backup_scan_and_failure_paths():
    blk = _wh_helpers()
    body = blk[blk.index("_wh_backup()"):blk.index("_wh_restore()")]
    assert "pg_restore --list" in body
    assert "CREATE SERVER" in body and "USER MAPPING" in body
    assert ".sha256" in body and "_prune_wh_backups" in body
    assert body.count("return 0") >= 4
    assert "return 1" not in body and "exit " not in body


def test_p14_backup_call_after_mv():
    blk = _block()
    assert blk.count("_wh_backup") == 1
    assert blk.index('mv "$BF.partial" "$BF"') < blk.index("_wh_backup")
    line = [ln for ln in blk.splitlines() if "_wh_backup" in ln][0]
    assert "# Phase 14: WH-09" in line


def test_p14_restore_dispatch_and_scratch():
    blk = _block()
    i_chk = blk.index('[[ -f "$RS_FILE" ]]')
    i_dis = blk.index("_wh_restore", i_chk)
    assert i_dis < blk.index("_valid_db", i_chk)
    h = _wh_helpers()
    r = h[h.index("_wh_restore()"):]
    for frag in ("warehouse_restore", "--no-owner --no-acl --exit-on-error", "--role=wh_owner",
                 "--clean --if-exists", "dropdb", "createdb", "-T template0", "--force",
                 "ic_meta.source_catalog", "ic_meta.link_state"):
        assert frag in r, frag
    assert not re.search(r"DROP DATABASE", CTL, re.I)


def test_p14_warehouse_arm():
    import re as _re
    assert len(_re.findall(r"^  warehouse\)", CTL, _re.M)) == 1
    assert CTL.index("# Phase 14: WH-09 / D-09 warehouse") < CTL.index("  selftest)")
    arm = _wh_arm()
    i_up = arm.index("docker compose up -d --build warehouse")
    i_poll = arm.index("healthy")
    i_set = arm.index("WAREHOUSE_URL=postgresql://127.0.0.1:5434/warehouse")
    assert i_up < i_poll < i_set
    assert "ensure-warehouse-secrets.sh" in arm
    assert "enable" in arm and "disable" in arm and "status" in arm
    assert "link_state" in arm and "systemctl restart agentic-api" in arm
    # only the WAREHOUSE_URL line is edited; password keys are never written
    assert "_PASSWORD" not in arm and "_OWNER_PASSWORD" not in arm
    assert not re.search(r"echo .*\$\{?WH_[A-Z]*PASSWORD", arm)


def test_p14_usage_line():
    assert "warehouse enable|disable|status" in CTL
    assert "backup [--dir d] [--keep-days n]|restore <file> [--into db] [--force]" in CTL
