"""Agent Gateway: receives WhatsApp webhooks, queues tasks, serves ops API."""

import asyncio
import hmac
import inspect
import logging
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from . import connectors, db, llmclient, logbuffer, secretbox, sentinel, worker
from .config import get_settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")


# Boot state. The dashboard is this product's only diagnostic surface, so the
# HTTP listener must come up even when the working-memory DB does not — an
# operator who can read "database unreachable" can fix it; one staring at a
# systemd crash loop cannot.
APP_STATE: dict = {"db_ready": False, "degraded_reason": None}
_background: list = []


async def _init_schema_with_retry(attempts: int = 30, delay: float = 2.0) -> bool:
    """Bounded retry: the gateway routinely starts before its own containers
    (docker.service being up does not mean compose services are), and must also
    survive a Postgres restart under it."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            await db.init_schema()
            return True
        except Exception as e:
            last = e
            if i == 0:
                log.warning("working-memory DB not ready (%s: %s) — retrying", type(e).__name__, e)
            if i < attempts - 1:
                await asyncio.sleep(delay)
    APP_STATE["degraded_reason"] = (
        f"working-memory database unreachable ({type(last).__name__}: {last}). "
        "On the VM: `cd <repo> && docker compose up -d`, then `agentctl status`."
    )
    log.error("%s", APP_STATE["degraded_reason"])
    return False


async def _startup():
    """Bring subsystems up without ever blocking the HTTP listener."""
    while not await _init_schema_with_retry():
        await asyncio.sleep(30)   # keep trying, so a late `compose up` self-heals
    APP_STATE["db_ready"] = True
    APP_STATE["degraded_reason"] = None
    # Apply dashboard-saved config overrides (system_configs) before any loop
    # reads settings. The app must boot fine with ZERO configuration.
    from . import configservice
    try:
        await configservice.load_overrides()
    except Exception:
        log.exception("config override load failed (continuing with .env/defaults)")
    # Quick 020: give preferences stored before scopes existed their person
    # (after the overrides, so owner_number is known). Idempotent, never raises.
    from . import memscope
    await memscope.migrate_preference_scopes()
    # ...and take the preferences earlier versions mirrored out of the shared
    # graph, once it has connected (bounded background task, never raises).
    _background.append(asyncio.create_task(memscope.retire_preference_graph_soon()))
    # Quick 024: cases opened for a request to the assistant before its replies
    # could close them get their commitments; the triage loop's satisfaction sweep
    # then closes the answered ones quietly. Idempotent, never raises, no LLM here.
    try:
        from . import triage
        await triage.migrate_assistant_requests()
    except ImportError:
        pass
    # Reactive pipeline (unchanged) + ambient loops. All follow the same
    # crash-tolerant loop conventions (backoff, no unhandled-exception death).
    from . import usage  # Quick 005: usage ledger writer + daily budget alert
    from . import jobruns  # Quick 008: background runs left 'running' by a dead process
    await jobruns.recover()  # never raises
    _background.extend([
        asyncio.create_task(usage.run_forever()),
        asyncio.create_task(worker.run_forever()),
        asyncio.create_task(sentinel.run_forever()),
        asyncio.create_task(connectors.run_forever()),
        *[asyncio.create_task(loop()) for loop in _ambient_loops()],
    ])
    from . import secretbox
    secretbox.install_log_redaction()  # Quick 029 F7: handlers added while modules loaded
    log.info("gateway ready — %d background loops running", len(_background))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logbuffer.install()
    starter = asyncio.create_task(_startup())
    yield
    starter.cancel()
    for t in _background:
        t.cancel()
    from . import usage
    await usage.drain()  # Quick 005: write queued ledger rows before the pool closes
    await db.close_pool()


def _ambient_loops() -> list:
    """Optional ambient loops, wired in as later phases land. Kept import-soft so
    each phase's branch stays runnable on its own."""
    loops = []
    try:
        from . import triage
        loops.append(triage.run_forever)
    except ImportError:
        pass
    try:
        from . import expectations
        loops.append(expectations.run_forever)
    except ImportError:
        pass
    try:
        from . import docintel
        loops.append(docintel.run_forever)   # watched inbox folder intake
    except ImportError:
        pass
    try:
        from . import retention
        loops.append(retention.run_forever)  # artifact disk retention
    except ImportError:
        pass
    # Phase 4: dashboard snapshot refresh (SERVE-05); import-soft like the loops above
    try:
        from . import dashboard_refresh
        loops.append(dashboard_refresh.run_forever)
    except ImportError:
        pass
    # end Phase 4
    # Phase 11: health alerts (GOLIVE-05); import-soft like the loops above
    try:
        from . import healthwatch
        loops.append(healthwatch.run_forever)
    except ImportError:
        pass
    # end Phase 11
    # Quick 009: connect the knowledge graph at startup, notice when it goes
    # away, reconnect on a backoff schedule (no restart needed)
    try:
        from .memoryservice import memory_graph
        loops.append(memory_graph.run_forever)
    except ImportError:
        pass
    # Phase 14: warehouse bootstrap and business_db link (WH-04, WH-09); import-soft like the loops above
    try:
        from . import warehouse
        loops.append(warehouse.run_forever)
    except ImportError:
        pass
    # end Phase 14
    # Phase 15: data source scheduler sweep (SRC-05); import-soft like the loops above
    try:
        from . import syncsched
        loops.append(syncsched.run_forever)
    except ImportError:
        pass
    # end Phase 15
    return loops


app = FastAPI(title="Agentic System Gateway", lifespan=lifespan)


@app.get("/health")
async def health():
    # Deliberately 200 even when degraded: agentctl's `curl -fsS` must reach the
    # body to print WHY, rather than collapsing every fault to "NOT RESPONDING".
    ui_profile = get_settings().ui_profile  # Phase 8: sign-in screen learns the profile
    if APP_STATE["db_ready"]:
        return {"ok": True, "ui_profile": ui_profile}
    return {
        "ok": False,
        "degraded": True,
        "reason": APP_STATE["degraded_reason"] or "initialising working-memory schema",
        "ui_profile": ui_profile,
    }


@app.get("/dashboard")
async def dashboard():
    from pathlib import Path

    from fastapi.responses import HTMLResponse
    return HTMLResponse((Path(__file__).parent / "dashboard.html").read_text())


async def _ask_primary_about_chat() -> None:
    """Ask the primary user what a newly-seen group is for.

    Best-effort and silent on failure: this runs inside the webhook, where
    nothing may break message intake. One question at a time — the answer comes
    back as a plain DM with no way to say which group it refers to.
    """
    try:
        from . import chats, people, whatsapp
        pending = await chats.next_unasked()
        if not pending:
            return
        primary = await people.primary_person()
        if not primary or not primary.get("whatsapp"):
            log.warning("group %s needs onboarding but no primary user is set "
                        "(People tab, or set owner_number)", pending["jid"])
            return
        await whatsapp.send_message(
            primary["whatsapp"],
            chats.ONBOARDING_QUESTION.format(
                subject=pending.get("subject") or pending["jid"]))
        await chats.mark_asked(pending["jid"])
    except Exception:
        log.exception("onboarding ask failed (message intake unaffected)")


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    x_webhook_secret: str = Header(default=""),
):
    settings = get_settings()
    if not settings.webhook_secret or not hmac.compare_digest(x_webhook_secret, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="bad secret")

    body = await request.json()
    phone = str(body.get("phone", "")).strip()
    text = (body.get("message") or "").strip()
    # Phase 15 (SRC-02, gap 1): pasted secrets never reach messages or the agent
    text = secretbox.scrub_pasted_secrets(text)[0]
    wa_message_id = body.get("messageId")

    # Quick 017: WhatsApp may hide a sender's number behind a private ID (LID).
    # The connector then sends `lid` (and `phone` empty until the number is
    # revealed). wasenders.py owns the allowlist rule, the LID map and the
    # blocked-senders record.
    from . import wasenders
    lid = wasenders.digits(body.get("lid"))
    if wasenders.is_lid_jid(phone):           # a LID passed in the phone field
        lid, phone = lid or wasenders.digits(phone), ""

    # Group context. `chat_jid` is the group JID; `mentioned` is the connector's
    # verdict on whether the message addressed us (always true for a DM).
    chat_jid = (body.get("chatId") or body.get("groupId") or "").strip() or None
    is_group = bool(body.get("isGroup")) and bool(chat_jid)
    mentioned = bool(body.get("mentioned", True))

    # Phase 8: WR-05 - the connector secret must not reach operator-trusted threads.
    from . import appchannel
    if appchannel.is_reserved_thread(phone) or appchannel.is_reserved_thread(chat_jid):
        raise HTTPException(status_code=422, detail="reserved thread id")

    # Document/media message: the connector may deliver an attachment either as
    # {"document": {"filename", "mimetype", "data"(b64)}} or flat as
    # {"filename", "mediaBase64"}. Text becomes the caption (or a synthetic
    # instruction) and the file is written into the new task's workspace.
    doc = body.get("document") if isinstance(body.get("document"), dict) else None
    media_b64 = (doc or {}).get("data") or body.get("mediaBase64")
    media_name = ((doc or {}).get("filename") or body.get("filename") or "").strip()

    if not (phone or lid) or (not text and not media_b64):
        raise HTTPException(status_code=422, detail="phone and message (or document) required")

    # Quick 017: remember the LID (with its phone when the message carried
    # both). A LID-only sender takes the phone on file; with none on file the
    # connector is asked for the LIDs of the allowlisted numbers and
    # owner_number (Baileys' documented PN -> LID lookup, throttled), so the
    # owner is recognised, threaded and answered as their phone number.
    # Fail-soft: the map being down only means the LID alone is judged.
    if lid:
        try:
            pool = await db.get_pool()
            async with pool.acquire() as conn:
                mapped = await wasenders.note_lid(conn, lid, phone)
                if not phone:
                    phone = mapped or await wasenders.match_by_lookup(conn, settings, lid)
        except Exception:
            log.exception("LID map unavailable (message still handled)")

    # Allowlist (quick 046: empty = owner only; open only via whatsapp_open_to_anyone): phone or LID listed, or owner_number.
    # A dropped sender is recorded for Setup > WhatsApp: who, when and the first
    # few characters only; the log names a masked number and never the text.
    if not wasenders.is_allowed(settings, phone=phone, lid=lid):
        log.warning("dropped message from non-allowlisted sender %s%s",
                    wasenders.mask(phone or lid), "" if phone else " (private ID)")
        try:
            pool = await db.get_pool()
            async with pool.acquire() as conn:
                await wasenders.record_blocked(
                    conn, phone=phone, lid=lid, name=body.get("name"), text=text,
                    chat_jid=chat_jid if is_group else None,
                    group_subject=body.get("groupSubject") if is_group else None)
        except Exception:
            log.exception("could not record the blocked sender")
        return {"queued": False, "reason": "not allowlisted"}

    # A sender known only by LID threads (and is answered) as "<lid>@lid";
    # once the phone is known, as the phone, so one person keeps one thread.
    if not phone:
        phone = wasenders.lid_address(lid)

    # Conversation identity. Groups thread on the group JID so history, replies
    # and echo detection all key on the same thing — and because worker.py sends
    # its reply TO thread_id, this is also what makes a group reply land in the
    # group instead of the sender's DM.
    thread_id = chat_jid if is_group else phone

    # Chat gating. A group is silent until a human activates it — being added to
    # one is not consent to act in it. Direct chats are self-evidently addressed
    # to us and are covered by the phone allowlist above.
    chat_active = True
    if is_group:
        try:
            from . import chats
            row = await chats.ensure_chat(
                chat_jid, subject=(body.get("groupSubject") or None), is_group=True)
            chat_active = row["status"] == "active"
            chats.maybe_sync_members(row)   # roster refresh; fire-and-forget
            if not chat_active:
                await _ask_primary_about_chat()
        except Exception:
            # Never let registry trouble drop a message: fall back to silent
            # observation, which is the safe direction.
            log.exception("chat registry failed for %s", chat_jid)
            chat_active = False
        if not chat_active:
            mentioned = False       # do not answer in an unapproved group

    # What triage should see is what a HUMAN said, plus a factual note that a
    # file arrived. The synthetic "extract this and propose the data entry"
    # below is an instruction aimed at the agent — feeding that to triage would
    # have it open cases about work the assistant invented for itself.
    event_text = text
    if media_b64:
        event_text = f"{text} [attachment: {media_name or 'file'}]".strip()

    if media_b64 and not text:
        text = (f"[document received: {media_name or 'file.pdf'}] "
                "Extract this document and propose the data entry.")
    elif media_b64:
        text = f"[document received: {media_name or 'file.pdf'}] {text}"

    # Reply anchoring: a swipe-reply carries the quoted message's text and
    # author (connector `quoted` field); fold a short factual note into what
    # BOTH pipelines see, so "done" replied to a week-old message still says
    # what it answers — the reply text alone carries no clue. The snippet is
    # untrusted chat text like everything else: flattened and capped.
    quoted = body.get("quoted")
    quote_who = quote_snip = None
    if isinstance(quoted, dict) and str(quoted.get("text") or "").strip():
        import re as _re
        quote_snip = _re.sub(r"\s+", " ", str(quoted.get("text"))).strip()[:200]
        # Phase 15 (SRC-02, gap 1): pasted secrets never reach messages or the agent
        quote_snip = secretbox.scrub_pasted_secrets(quote_snip)[0]
        quote_who = "the assistant" if quoted.get("from_me") else (
            str(quoted.get("author") or "").strip() or "someone")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if quote_snip:
            if quote_who not in ("the assistant", "someone"):
                # Prefer a registered name over a bare number; the number is a
                # fine fallback and failure here must never drop the message.
                try:
                    from . import people as _people
                    nm = await conn.fetchval(
                        """SELECT name FROM people
                           WHERE active AND (whatsapp = $1 OR $1 = ANY(whatsapp_alt))
                           LIMIT 1""", _people.normalize_phone(quote_who))
                    if nm:
                        quote_who = nm
                except Exception:
                    pass
            note = f'[replying to {quote_who}: "{quote_snip}"] '
            text = note + text
            event_text = note + event_text

        await conn.execute(
            "INSERT INTO threads (thread_id, phone) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            thread_id,
            phone,
        )
        msg_id = await conn.fetchval(
            """INSERT INTO messages (thread_id, role, content, wa_message_id)
               VALUES ($1, 'user', $2, $3)
               ON CONFLICT (wa_message_id) DO NOTHING
               RETURNING id""",
            thread_id,
            text,
            wa_message_id,
        )
        if msg_id is None:  # duplicate delivery
            return {"queued": False, "reason": "duplicate"}

        # Observe always, speak selectively: a group message that doesn't
        # address us is still recorded and still triaged below (that is the
        # whole point of an ambient assistant), but it must not generate a
        # reply — otherwise the agent answers every line in the group.
        run_id = None
        if mentioned:
            run_id = await conn.fetchval(
                "INSERT INTO task_runs (thread_id, message_id) VALUES ($1, $2) RETURNING id",
                thread_id,
                msg_id,
            )

    # Decode the attachment once: both the reactive workspace write and the
    # eager document intake below consume the same bytes.
    media_bytes = None
    if media_b64:
        try:
            import base64 as _b64
            media_bytes = _b64.b64decode(media_b64)
        except Exception:
            log.exception("failed to decode inbound document (message still handled)")

    # The workspace is keyed on the run, so an attachment can only be stored
    # when a run exists. An unaddressed group attachment is still recorded as an
    # event; it just isn't handed to the agents until someone asks about it.
    if media_bytes and run_id:
        try:
            from . import sandbox
            safe = sandbox.safe_name(media_name)
            (sandbox.scratch_dir(run_id) / safe).write_bytes(media_bytes)
        except Exception:
            log.exception("failed to store inbound document (task still queued)")

    # Ambient (additive): also record this as an event for triage. Independent of
    # the reactive path above — an ingest failure must never drop the task.
    try:
        from .connectors.whatsapp_events import ingest_inbound
        await ingest_inbound(
            phone=phone, text=event_text, wa_message_id=wa_message_id,
            name=(body.get("name") or None),
            group_jid=chat_jid, is_group=is_group,
            # A group nobody has approved is recorded but not acted on. The row
            # stays, so activating the chat later leaves a readable timeline.
            triage_status="pending" if chat_active else "skipped",
            # Quick 024: the assistant is answering this message (a reply run
            # exists), so triage may record the request as its commitment.
            to_assistant=run_id is not None)
    except Exception:
        log.exception("event ingest failed (reactive task still queued)")

    # Eager document understanding (additive): persist + queue extraction for
    # any attachment arriving in an active chat, mentioned or not, so triage
    # already knows the file when someone asks about it. Unapproved/pending
    # chats persist nothing — being added to a group is not consent to read
    # its files. Failure here must never affect the message flow.
    if media_bytes and chat_active:
        try:
            from pathlib import Path as _Path

            from . import docintel, sandbox
            safe = sandbox.safe_name(media_name)
            if _Path(safe).suffix.lower() in docintel.EAGER_EXTS:
                await docintel.intake_bytes(
                    media_bytes, safe, source="whatsapp", thread_ref=thread_id)
        except Exception:
            log.exception("eager document intake failed (message still handled)")
    return {"queued": bool(run_id), "observed": True, "chat_active": chat_active}


# ── Ops API (dashboard reads these) ─────────────────────────────────────
# Publicly exposed, so ops endpoints require the admin key (header: x-ops-key).
# OPS_KEY is distinct from WEBHOOK_SECRET (which the connector holds and which
# must not grant admin); webhook_secret is accepted as a fallback only for
# pre-split deployments that have no OPS_KEY yet.


_warned_webhook_fallback = False


def _require_ops_key(x_ops_key: str = Header(default="")):
    global _warned_webhook_fallback
    settings = get_settings()
    expected = settings.ops_key
    if not expected:
        # DEPRECATED fallback for deployments predating the OPS_KEY/WEBHOOK_SECRET
        # split. It means the WhatsApp connector's shared secret also grants full
        # admin access to /api/*. install.sh back-fills OPS_KEY on every run, so
        # any supported deployment can clear this by re-running the installer.
        # Kept (rather than removed) only so an un-upgraded VM cannot lock its
        # operator out of the dashboard, which is the only recovery surface.
        expected = settings.webhook_secret
        if expected and not _warned_webhook_fallback:
            _warned_webhook_fallback = True
            log.warning(
                "SECURITY: no OPS_KEY set — falling back to WEBHOOK_SECRET as the admin "
                "key, so the WhatsApp connector holds full /api access. Fix: add "
                "OPS_KEY=$(openssl rand -hex 32) to .env and restart, or re-run install.sh.")
    if not expected or not hmac.compare_digest(x_ops_key, expected):
        raise HTTPException(status_code=401, detail="bad ops key")


@app.get("/api/runs", dependencies=[Depends(_require_ops_key)])
async def list_runs(limit: int = 50):
    pool = await db.get_pool()
    rows = await pool.fetch(
        """SELECT id, thread_id, status, result_summary, error, created_at, started_at, ended_at
           FROM task_runs ORDER BY id DESC LIMIT $1""",
        limit,
    )
    out = [dict(r) for r in rows]
    # Quick 010: which skills each run loaded (use_skill tool calls, first-use
    # order). Fail-soft: a skills lookup failure must not break the runs list.
    try:
        from . import skillservice
        used = await skillservice.skills_used(pool, [r["id"] for r in out])
    except Exception:
        log.warning("skills_used lookup failed", exc_info=True)
        used = {}
    for r in out:
        r["skills_used"] = used.get(r["id"], [])
    return out


@app.get("/api/threads/{thread_id}/messages", dependencies=[Depends(_require_ops_key)])
async def thread_messages(thread_id: str, limit: int = 100):
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT id, role, content, created_at FROM messages WHERE thread_id = $1 ORDER BY id DESC LIMIT $2",
        thread_id,
        limit,
    )
    return [dict(r) for r in rows]


@app.get("/api/agents", dependencies=[Depends(_require_ops_key)])
async def list_agents():
    from .agents.graph import registry
    return await registry()


VALID_AGENT_KEYS = {"supervisor", "data", "analysis", "document", "drawing", "reflection"}
VALID_AGENT_KEYS = VALID_AGENT_KEYS | {"dashboard"}  # Phase 6: dashboard specialist config
VALID_AGENT_KEYS = VALID_AGENT_KEYS | {"context"}  # Phase 12: context specialist config


@app.put("/api/agents/{config_key}/config", dependencies=[Depends(_require_ops_key)])
async def update_agent_config(config_key: str, body: dict):
    import json as _json

    from .agents.graph import AGENT_TOOL_SUPERSET

    if config_key not in VALID_AGENT_KEYS:
        raise HTTPException(status_code=404, detail=f"unknown agent: {config_key}")

    cfg: dict = {}
    prompt = (body.get("prompt") or "").strip()
    if len(prompt) < 20:
        raise HTTPException(status_code=422, detail="prompt too short (min 20 chars)")
    cfg["prompt"] = prompt

    model = (body.get("model") or "auto").strip()
    if not model:
        model = "auto"
    cfg["model"] = model

    try:
        cfg["temperature"] = max(0.0, min(2.0, float(body.get("temperature", 0.2))))
        cfg["max_tokens"] = max(256, min(llmclient.MAX_TOKENS_CEILING, int(body.get("max_tokens", 4096))))
        cfg["recursion_limit"] = max(1, min(100, int(body.get("recursion_limit", 25))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="temperature/max_tokens/recursion_limit must be numeric")

    # Tools sent as the ENABLED subset; we persist the DISABLED complement so new
    # superset tools added later default ON.
    tools = body.get("tools")
    if tools is not None:
        superset = set(AGENT_TOOL_SUPERSET[config_key])
        illegal = [t for t in tools if t not in superset]
        if illegal:
            raise HTTPException(status_code=422,
                                detail=f"tools not in this agent's superset: {illegal}")
        cfg["disabled_tools"] = [t for t in superset if t not in set(tools)]

    pool = await db.get_pool()
    await pool.execute(
        """INSERT INTO agent_configs (agent_name, config) VALUES ($1, $2::jsonb)
           ON CONFLICT (agent_name) DO UPDATE SET config = $2::jsonb, prompt = NULL, updated_at = now()""",
        config_key, _json.dumps(cfg),
    )
    return {"saved": config_key, "applies": "next task"}


@app.delete("/api/agents/{config_key}/config", dependencies=[Depends(_require_ops_key)])
async def reset_agent_config(config_key: str):
    pool = await db.get_pool()
    await pool.execute("DELETE FROM agent_configs WHERE agent_name = $1", config_key)
    return {"reset": config_key}


@app.get("/api/stats", dependencies=[Depends(_require_ops_key)])
async def stats():
    import json as _json

    out: dict = {"process": {}, "system": {}, "containers": [], "queue": {}}

    try:
        import psutil
        p = psutil.Process()
        out["process"] = {
            "cpu_percent": p.cpu_percent(interval=None),
            "memory_mb": round(p.memory_info().rss / 1048576, 1),
            "threads": p.num_threads(),
        }
        vm = psutil.virtual_memory()
        out["system"] = {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": vm.percent,
            "memory_used_gb": round(vm.used / 1073741824, 1),
            "memory_total_gb": round(vm.total / 1073741824, 1),
        }
    except Exception as e:
        out["system"] = {"error": str(e)}

    # Docker containers (postgres, langfuse, live sandbox runs)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stats", "--no-stream", "--format", "{{json .}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        for line in stdout.decode().splitlines():
            c = _json.loads(line)
            out["containers"].append({
                "name": c.get("Name"), "cpu": c.get("CPUPerc"),
                "mem": c.get("MemUsage"), "mem_percent": c.get("MemPerc"),
            })
    except Exception:
        pass  # docker unavailable — containers list stays empty

    try:
        pool = await db.get_pool()
        rows = await pool.fetch("SELECT status, count(*) AS n FROM task_runs GROUP BY status")
        out["queue"] = {r["status"]: r["n"] for r in rows}
    except Exception:
        pass
    return out


@app.get("/api/logs", dependencies=[Depends(_require_ops_key)])
async def recent_logs(limit: int = 200):
    return logbuffer.recent(limit)


@app.get("/api/logs/stream", dependencies=[Depends(_require_ops_key)])
async def stream_logs():
    import json as _json

    from fastapi.responses import StreamingResponse

    async def gen():
        q = logbuffer.subscribe()
        try:
            for entry in logbuffer.recent(50):
                yield f"data: {_json.dumps(entry)}\n\n"
            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {_json.dumps(entry)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            logbuffer.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/watches", dependencies=[Depends(_require_ops_key)])
async def list_watches():
    pool = await db.get_pool()
    rows = await pool.fetch(
        """SELECT id, thread_id, kind, title, condition, cadence_seconds, one_shot,
                  status, last_state, compiled_sql, trigger_rule, compile_error,
                  last_checked_at, last_fired_at, fire_count, created_at,
                  measure_ref, stale_since, data_as_of,  -- Phase 17 (CONS-02)
                  -- Quick 006: the open alert case, if any
                  (SELECT c.id FROM cases c WHERE c.origin_ref = 'watch:' || w.id::text
                      AND c.status IN ('open', 'pending_ack') LIMIT 1) AS open_case_id
           FROM watches w ORDER BY id DESC""")
    import json as _json
    out = [dict(r) for r in rows]
    for r in out:  # Phase 17 (CONS-02): jsonb comes back as text
        if isinstance(r.get("measure_ref"), str):
            r["measure_ref"] = _json.loads(r["measure_ref"])
    return out


@app.post("/api/watches", dependencies=[Depends(_require_ops_key)])
async def create_watch_api(body: dict):
    required = ("thread_id", "title", "condition")
    if not all(body.get(k) for k in required):
        raise HTTPException(status_code=422, detail=f"required: {required}")
    pool = await db.get_pool()
    wid = await pool.fetchval(
        """INSERT INTO watches (thread_id, kind, title, condition, cadence_seconds, one_shot)
           VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
        str(body["thread_id"]), body.get("kind", "user"), body["title"][:120],
        body["condition"], max(300, int(body.get("cadence_seconds", 900))),
        bool(body.get("one_shot", False)))
    return {"created": wid}


@app.patch("/api/watches/{watch_id}", dependencies=[Depends(_require_ops_key)])
async def update_watch(watch_id: int, body: dict):
    status = body.get("status")
    if status not in ("active", "paused", "done"):
        raise HTTPException(status_code=422, detail="status must be active|paused|done")
    pool = await db.get_pool()
    await pool.execute("UPDATE watches SET status=$2 WHERE id=$1", watch_id, status)
    closed = 0
    if status in ("paused", "done"):  # Quick 006: nothing checks its alert case any more
        closed = await _close_watch_cases(pool, watch_id, status)
    return {"updated": watch_id, "status": status, "cases_closed": closed}


@app.delete("/api/watches/{watch_id}", dependencies=[Depends(_require_ops_key)])
async def delete_watch(watch_id: int):
    pool = await db.get_pool()
    closed = await _close_watch_cases(pool, watch_id, "deleted")  # Quick 006: before the row goes
    await pool.execute("DELETE FROM watches WHERE id=$1", watch_id)
    return {"deleted": watch_id, "cases_closed": closed}


async def _close_watch_cases(pool, watch_id: int, why: str) -> int:
    """Quick 006: close a stopped watch's open alert case now; fail-soft, because the
    sentinel sweep closes it on its next pass anyway."""
    try:
        from . import findingcases
        return await findingcases.close_for_watch(watch_id, why, pool=pool)
    except Exception:
        log.exception("closing the alert case of watch %s failed", watch_id)
        return 0


@app.get("/api/watches/{watch_id}/events", dependencies=[Depends(_require_ops_key)])
async def watch_events(watch_id: int, limit: int = 20):
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT severity, summary, escalated, created_at, kind FROM watch_events WHERE watch_id=$1 ORDER BY id DESC LIMIT $2",
        watch_id, limit)
    return [dict(r) for r in rows]


# ── Phase 5: KPI history ──
@app.get("/api/watches/{watch_id}/history", dependencies=[Depends(_require_ops_key)])
async def watch_history(watch_id: int, days: int = 30):
    """Recorded observations of one watch over the last `days` days. The SAME
    series the `get_kpi_history` agent tool returns (kpihistory.series)."""
    from . import kpihistory
    if not kpihistory.MIN_DAYS <= days <= kpihistory.MAX_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"days must be {kpihistory.MIN_DAYS}..{kpihistory.MAX_DAYS}")
    out = await kpihistory.series(watch_id, days)
    if out is None:
        raise HTTPException(status_code=404, detail="watch not found")
    return out


@app.post("/api/watches/propose-kpi", dependencies=[Depends(_require_ops_key)])
async def propose_kpi_watches(body: dict):
    """Propose anomaly watches from the owner-approved KPI definitions.

    Phase 12 (CTX-09b): approved business_context KPIs, newest first, skipping
    titles already watched; neutral KPI search as a fallback."""
    from . import insights
    from .knowledge import approved_kpis, search_business_context
    pool = await db.get_pool()
    try:
        rows = await pool.fetch("SELECT title FROM watches WHERE status <> 'done'")
        watched = {insights.norm_title(r["title"]) for r in rows}
    except Exception:
        watched = set()
    cands = [(k.get("title"), k.get("body") or "") for k in await approved_kpis(12)]
    if not cands:
        hits = await search_business_context("key performance indicator KPI metric", limit=12)
        cands = [(h.get("title") or h.get("kpi_name"), h.get("body") or h.get("text") or "")
                 for h in hits if (h.get("kind") or h.get("type")) == "kpi"]
    seen, proposals = set(), []
    for name, text in cands:
        key = insights.norm_title(name) if name else ""
        if not key or key in seen or key in watched:
            continue
        seen.add(key)
        proposals.append({
            "title": name,
            "condition": f"Monitor the KPI '{name}': {text[:400]}. "
                         "Flag a notable change or anomaly versus the previous observation.",
            "kind": "kpi",
            "cadence_seconds": 3600,
        })
    return {"proposals": proposals[:8]}


@app.get("/api/learnings", dependencies=[Depends(_require_ops_key)])
async def list_learnings(limit: int = 100):
    pool = await db.get_pool()
    rows = await pool.fetch(
        # Quick 009: status + status_reason so the Memory page can show a
        # superseded learning as retired (and why) instead of as live.
        """SELECT id, thread_id, kind, content, created_at, updated_at, revisions,
                  status, status_reason, graphed_at, source, scope
           FROM learnings ORDER BY updated_at DESC LIMIT $1""",
        limit,
    )
    # Quick 020: who each learning applies to (scope_kind owner | person |
    # group | global, scope_label), names resolved now; never raises.
    from . import memscope
    return await memscope.describe([dict(r) for r in rows])


@app.delete("/api/learnings/{learning_id}", dependencies=[Depends(_require_ops_key)])
async def delete_learning(learning_id: int):
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "DELETE FROM learnings WHERE id = $1 RETURNING kind, content", learning_id)
    graph = None
    if row is not None:
        # Quick 019: the learning's graph facts go with it (facts other
        # episodes also support stay). Fail-soft: a graph that cannot be
        # reached is recorded in job_runs; a rebuild cleans it up.
        from . import memory
        graph = await memory.retire_learning_graph(
            learning_id, kind=row["kind"], content=row["content"], pool=pool)
        # Quick 020: a forgotten pending learning takes its approval card with it.
        try:
            await pool.execute(
                "UPDATE actions SET status = 'rejected' WHERE kind = 'activate_learning' "
                "AND status = 'proposed' AND payload->>'learning_id' = $1", str(learning_id))
        except Exception:
            log.warning("could not close the approval card of learning %s", learning_id,
                        exc_info=True)
    return {"deleted": learning_id, "graph": graph}


@app.get("/api/memory/episodes", dependencies=[Depends(_require_ops_key)])
async def memory_episodes(limit: int = 30):
    """Raw episodes in the knowledge graph, newest first — the browse surface
    for what onboarding, case distillation and chat setup actually wrote.
    Fail-soft: an empty list when the graph is down, never an error."""
    from . import memoryservice
    return {"episodes": await memoryservice.memory_graph.recent_episodes(
        limit=max(1, min(limit, 200)))}


@app.get("/api/memory/status", dependencies=[Depends(_require_ops_key)])
async def memory_status():
    """Quick 009: is the knowledge graph connected, empty, or not connected
    (and why); what a backfill would send; the last backfill's progress.
    Pings a connected graph and attempts a due retry, so it is never stale."""
    from . import memory_pipeline
    return await memory_pipeline.graph_status()


@app.post("/api/memory/reconnect", dependencies=[Depends(_require_ops_key)])
async def memory_reconnect():
    """Quick 009: drop the graph client and try to connect right now."""
    from . import memory_pipeline, memoryservice
    await memoryservice.memory_graph.reconnect()
    return await memory_pipeline.graph_status(probe=False)


@app.post("/api/memory/backfill", dependencies=[Depends(_require_ops_key)])
async def memory_backfill(limit: int = 50):
    """Quick 009: send learnings and closed cases that never reached the graph
    (idempotent, bounded by limit, max 200). Runs in the background; progress
    is in GET /api/memory/status under "backfill"."""
    from . import memory_pipeline, memoryservice
    if not await memoryservice.memory_graph._ensure():
        st = await memory_pipeline.graph_status(probe=False)
        raise HTTPException(409, f"knowledge graph not connected: {st.get('reason') or 'unknown'}")
    started = memory_pipeline.start_backfill(limit)
    st = await memory_pipeline.graph_status(probe=False)
    return {"started": started, "already_running": not started, **st}


@app.get("/api/memory/search", dependencies=[Depends(_require_ops_key)])
async def memory_search(q: str = "", limit: int = 12, superseded: bool = True):
    """Search the knowledge graph the way agents do — the same recall over
    facts, entities and episodes (Quick 014) — but across every scope at
    once: the operator asking "what does it know" means globally AND
    per-chat/case. Superseded facts come back flagged (agents never see
    them); superseded=false drops them."""
    from . import memoryservice
    scope = await memoryservice.memory_graph.group_ids() or None
    return {"facts": await memoryservice.memory_graph.recall(
        q, scope=scope, limit=max(1, min(limit, 25)), include_superseded=superseded)}


@app.post("/api/memory/rebuild", dependencies=[Depends(_require_ops_key)])
async def memory_rebuild_start(dry_run: bool = False, max_distill: int = 50):
    """Quick 014: rebuild the knowledge graph from its Postgres sources
    (learnings, closed cases, onboarding, archived chat setup): archive,
    clear, re-ingest with the business entity types, clean junk and
    duplicates. Background, single-flight with the backfill; progress and
    the result are in GET /api/memory/status under "rebuild". dry_run plans
    without changing the graph."""
    from . import memory_pipeline, memory_rebuild, memoryservice
    if not await memoryservice.memory_graph._ensure():
        st = await memory_pipeline.graph_status(probe=False)
        raise HTTPException(409, f"knowledge graph not connected: {st.get('reason') or 'unknown'}")
    started = memory_rebuild.start_rebuild(dry_run=dry_run,
                                           max_distill=max(0, min(max_distill, 500)))
    st = await memory_pipeline.graph_status(probe=False)
    return {"started": started, "already_running": not started, **st}


@app.get("/api/memory/graph", dependencies=[Depends(_require_ops_key)])
async def memory_graph_view(scope: str = "everything", q: str = "", limit: int = 300,
                            episodes: bool = False, superseded: bool = True):
    """Quick 014: the knowledge graph as nodes and edges for the Memory
    page's graph view. Read-only, no free-form query. scope: everything,
    global, case:<id> or a chat graph; q highlights matches (the same recall
    agents use) and always includes them and their neighbours; limit caps
    the entities (most connected first); episodes adds episode nodes."""
    from . import graphview
    return await graphview.build_view(scope=scope, q=q, limit=limit,
                                      episodes=episodes, superseded=superseded)


@app.get("/api/memory/graph/node/{uuid}", dependencies=[Depends(_require_ops_key)])
async def memory_graph_node(uuid: str, group: str = "global"):
    """Quick 014: one graph node for the side panel and for expanding its
    neighbourhood: facts in and out, the episodes that mention it, links to
    case, KPI and table pages."""
    from . import graphview
    detail = await graphview.node_detail(group, uuid)
    if detail is None:
        raise HTTPException(404, "no such node in that scope")
    return detail


@app.get("/api/runs/{run_id}/tools", dependencies=[Depends(_require_ops_key)])
async def run_tool_calls(run_id: int):
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT tool, args, result_digest, latency_ms, created_at FROM tool_calls WHERE task_run_id = $1 ORDER BY id",
        run_id,
    )
    return [dict(r) for r in rows]


@app.get("/api/runs/{run_id}/artifacts", dependencies=[Depends(_require_ops_key)])
async def run_artifacts(run_id: int):
    """Files a run produced. Until this existed, WhatsApp's send_document was the
    only way anything left the workspace — so a CAD deconstruction started from
    the dashboard or the inbox folder wrote a dozen files nothing could fetch."""
    from . import sandbox
    d = sandbox.ARTIFACTS_ROOT / f"run_{run_id}"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file() and p.name not in {"script.py", ".runner.py"}:
            out.append({"name": p.name, "bytes": p.stat().st_size})
    return out


@app.get("/api/runs/{run_id}/artifacts/{name}", dependencies=[Depends(_require_ops_key)])
async def run_artifact(run_id: int, name: str):
    from pathlib import Path

    from fastapi.responses import FileResponse

    from . import sandbox
    # Same containment rule as the agent's _work_path: a bare filename only,
    # then a resolved-path check. `name` arrives from the URL, so it is fully
    # caller-controlled even though the route is ops-key gated.
    if not name or Path(name).name != name:
        raise HTTPException(status_code=400, detail="invalid artifact name")
    d = (sandbox.ARTIFACTS_ROOT / f"run_{run_id}").resolve()
    p = (d / name).resolve()
    if d not in p.parents or not p.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(p, filename=name)


# ── System configuration (dashboard Setup page) ─────────────────────────


@app.get("/api/config", dependencies=[Depends(_require_ops_key)])
async def api_get_config():
    from . import configservice
    return await configservice.list_config()


@app.put("/api/config", dependencies=[Depends(_require_ops_key)])
async def api_put_config(body: dict):
    from . import configservice
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        raise HTTPException(status_code=422, detail="body must be {values: {key: value}}")
    try:
        saved = await configservice.save_many(values)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"saved": saved}


@app.get("/api/config/status", dependencies=[Depends(_require_ops_key)])
async def api_config_status():
    from . import configservice
    return await configservice.status()


@app.post("/api/config/test/{group}", dependencies=[Depends(_require_ops_key)])
async def api_config_test(group: str):
    from . import configservice
    try:
        return await configservice.test_group(group)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Document intelligence ────────────────────────────────────────────────


CONTEXT_PACK_MAX_BYTES = 2_000_000  # Phase 12 (CTX-08): cap for context-pack extensions


@app.post("/api/documents/upload", dependencies=[Depends(_require_ops_key)])
async def api_upload_document(request: Request):
    """Dashboard/API intake: multipart file (+ optional 'instruction' and 'operator'
    fields). Queues a normal agent task with the file in its workspace — same path a
    WhatsApp attachment takes. Thread is 'dashboard', or ui:<operator> when the
    optional operator field is given (Phase 12: the owner's Ask thread)."""
    from . import appchannel, contextpack, sandbox
    form = await request.form()
    up = form.get("file")
    if up is None or not getattr(up, "filename", ""):
        raise HTTPException(status_code=422, detail="multipart 'file' required")
    allowed = (".pdf", ".jpg", ".jpeg", ".png", ".webp",
               ".xlsx", ".docx", ".csv", ".txt", ".xls",
               ".md", ".json", ".yaml", ".yml")  # Phase 12 (CTX-08): context packs
    if not up.filename.lower().endswith(allowed):
        raise HTTPException(status_code=422,
                            detail=f"supported document types: {allowed}")
    instruction = str(form.get("instruction") or
                      "Extract this document and propose the data entry.").strip()
    # Phase 15 (SRC-02, gap 1): pasted secrets never reach messages or the agent
    instruction = secretbox.scrub_pasted_secrets(instruction)[0]

    # Phase 12 (CTX-08): read + sanitize first, cap context packs, route by operator.
    data = await up.read()
    safe = sandbox.safe_name(up.filename)
    if safe.lower().endswith(contextpack.CONTEXT_PACK_EXTS) and len(data) > CONTEXT_PACK_MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"context-pack files are limited to {CONTEXT_PACK_MAX_BYTES} bytes")
    operator = form.get("operator")
    if operator is None or str(operator).strip() == "":
        thread_id = "dashboard"
    else:
        op = str(operator).strip()
        if not appchannel.OPERATOR_RE.match(op):
            raise HTTPException(status_code=422, detail="invalid operator")
        thread_id = appchannel.thread_for(op)

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO threads (thread_id, phone) VALUES ($1, $1) "
                "ON CONFLICT DO NOTHING", thread_id)
            msg_id = await conn.fetchval(
                "INSERT INTO messages (thread_id, role, content) VALUES ($1, 'user', $2) RETURNING id",
                thread_id, f"[document received: {safe}] {instruction}")
            run_id = await conn.fetchval(
                "INSERT INTO task_runs (thread_id, message_id) VALUES ($1, $2) RETURNING id",
                thread_id, msg_id)
    (sandbox.scratch_dir(run_id) / safe).write_bytes(data)
    return {"queued": run_id, "filename": safe, "thread_id": thread_id,  # Quick 003: panel opens on it
            "note": "watch /api/runs; extraction proposes actions for approval"}


@app.get("/api/documents", dependencies=[Depends(_require_ops_key)])
async def api_list_documents(limit: int = 50):
    pool = await db.get_pool()
    rows = await pool.fetch(
        """SELECT id, thread_id, task_run_id, filename, source, doc_type, summary,
                  status, created_at FROM documents ORDER BY id DESC LIMIT $1""", limit)
    return [dict(r) for r in rows]


@app.get("/api/documents/{document_id}", dependencies=[Depends(_require_ops_key)])
async def api_document_detail(document_id: int):
    pool = await db.get_pool()
    row = await pool.fetchrow("SELECT * FROM documents WHERE id = $1", document_id)
    if not row:
        raise HTTPException(status_code=404, detail="document not found")
    out = dict(row)
    records = await pool.fetch(
        "SELECT id, record, approved_by, created_at FROM extracted_records WHERE document_id = $1",
        document_id)
    out["records"] = [dict(r) for r in records]
    return out


# ── People registry (human-managed; system never auto-creates) ──────────


@app.get("/api/people", dependencies=[Depends(_require_ops_key)])
async def api_list_people():
    from . import people
    return await people.list_people()


@app.post("/api/people", dependencies=[Depends(_require_ops_key)])
async def api_create_person(body: dict):
    from . import people
    name = (body.get("name") or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=422, detail="name required (min 2 chars)")
    try:
        pid = await people.create_person(
            name=name, aliases=body.get("aliases") or [],
            email=body.get("email"), whatsapp=body.get("whatsapp"),
            role=body.get("role"), whatsapp_alt=body.get("whatsapp_alt") or [])
    except Exception as e:  # unique violation on email/whatsapp
        raise HTTPException(status_code=409, detail=f"could not create: {type(e).__name__}")
    linked = await people.reresolve_events()
    return {"created": pid, "reresolved_events": linked}


@app.patch("/api/people/{person_id}", dependencies=[Depends(_require_ops_key)])
async def api_update_person(person_id: int, body: dict):
    from . import people
    allowed = {k: body[k] for k in ("name", "aliases", "email", "whatsapp",
                                    "whatsapp_alt", "role", "active")
               if k in body}
    if not allowed:
        raise HTTPException(status_code=422, detail="no updatable fields provided")
    try:
        await people.update_person(person_id, allowed)
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"could not update: {type(e).__name__}")
    linked = await people.reresolve_events()
    return {"updated": person_id, "reresolved_events": linked}


@app.post("/api/people/{person_id}/primary", dependencies=[Depends(_require_ops_key)])
async def api_set_primary(person_id: int):
    """Flag the one person the assistant reports to (onboarding, digests,
    escalations). Single-winner: setting a new primary clears the old one."""
    from . import people
    await people.set_primary(person_id)
    return {"ok": True, "primary": person_id}


@app.get("/api/onboarding", dependencies=[Depends(_require_ops_key)])
async def api_onboarding_state(kind: str = "business", jid: str = ""):
    """Current interview state. `kind=business` once per deployment;
    `kind=chat&jid=…` once per group."""
    from . import onboarding
    session = await onboarding.get_session(kind, jid or None)
    if not session:
        return {"status": "not_started", "transcript": []}
    return {"status": session["status"], "transcript": session["transcript"],
            "profile": session["profile"], "updated_at": session["updated_at"]}


@app.post("/api/onboarding", dependencies=[Depends(_require_ops_key)])
async def api_onboarding_send(body: dict):
    """Advance the interview. Omit `message` to open it — the interviewer
    speaks first, which is what makes it an interview rather than a blank box."""
    from . import onboarding
    kind = (body.get("kind") or "business").strip()
    if kind not in ("business", "chat"):
        raise HTTPException(status_code=400, detail="kind must be business|chat")
    jid = (body.get("jid") or "").strip() or None
    if kind == "chat" and not jid:
        raise HTTPException(status_code=400, detail="chat onboarding needs a jid")
    return await onboarding.send(kind, body.get("message"), subject_ref=jid)


@app.post("/api/onboarding/finalize", dependencies=[Depends(_require_ops_key)])
async def api_onboarding_finalize(body: dict):
    """Force the interviewer to save its profile NOW (forced tool call) — the
    escape hatch for a model that promises to save in prose and never does."""
    from . import onboarding
    kind = (body.get("kind") or "business").strip()
    if kind not in ("business", "chat"):
        raise HTTPException(status_code=400, detail="kind must be business|chat")
    jid = (body.get("jid") or "").strip() or None
    if kind == "chat" and not jid:
        raise HTTPException(status_code=400, detail="chat onboarding needs a jid")
    return await onboarding.finalize(kind, subject_ref=jid)


@app.post("/api/onboarding/reset", dependencies=[Depends(_require_ops_key)])
async def api_onboarding_reset(body: dict):
    """Abandon the live interview so a fresh one can start. Completed sessions
    are kept — they are the provenance of an applied profile."""
    from . import onboarding
    kind = (body.get("kind") or "business").strip()
    await onboarding.reset(kind, (body.get("jid") or "").strip() or None)
    return {"ok": True}


@app.get("/api/chats", dependencies=[Depends(_require_ops_key)])
async def api_list_chats():
    from . import chats
    return {"chats": await chats.list_chats()}


@app.patch("/api/chats/{jid}", dependencies=[Depends(_require_ops_key)])
async def api_update_chat(jid: str, body: dict):
    """Activate / ignore a chat, or edit its purpose. Activation is the human
    approval that lets the ambient layer act on that group at all."""
    from . import chats
    status = (body.get("status") or "").strip() or None
    purpose = body.get("purpose")
    if status and status not in ("pending", "active", "ignored"):
        raise HTTPException(status_code=400, detail="bad status")
    if status is None and purpose is None:
        raise HTTPException(status_code=400, detail="nothing to update")
    # Omitting `status` keeps the current one. It must NOT fall back to
    # 'pending': editing only the purpose of an active group would then silence
    # a group a human had already approved.
    await chats.set_status(jid, status, purpose=purpose)
    return {"ok": True}


@app.get("/api/chats/{jid}/members", dependencies=[Depends(_require_ops_key)])
async def api_chat_members(jid: str):
    """The synced roster, with person identity resolved at read time."""
    from . import chats
    return {"members": await chats.list_members(jid)}


@app.post("/api/chats/{jid}/sync-members", dependencies=[Depends(_require_ops_key)])
async def api_sync_chat_members(jid: str):
    """Refresh the roster from the connector now (the Chats-tab button)."""
    from . import chats
    if not jid.endswith("@g.us"):
        raise HTTPException(status_code=400, detail="not a group jid")
    pool = await db.get_pool()
    # Also protects the chat_members FK: a sync for an unknown jid would
    # otherwise fail on the first insert.
    if not await pool.fetchval("SELECT 1 FROM chats WHERE jid = $1", jid):
        raise HTTPException(status_code=404, detail="unknown chat")
    n = await chats.sync_members(jid)
    if n is None:
        raise HTTPException(status_code=502,
                            detail="connector unavailable or group not accessible")
    return {"synced": n}


@app.get("/api/whatsapp/qr", dependencies=[Depends(_require_ops_key)])
async def api_whatsapp_qr():
    """Pairing state for the dashboard's WhatsApp page: connected + linked
    account, or the current QR as a data URL to scan. Proxied through the
    gateway because the connector is loopback-only on client VMs."""
    from . import whatsapp
    data = await whatsapp.fetch_qr()
    if data is None:
        return {"reachable": False, "connected": False, "qr": None, "user": None}
    return {"reachable": True, **data}


# Quick 017: messages the allowlist dropped, and the owner's "Allow this sender"
# (adds the phone or private ID to allowed_numbers through configservice.save_many).
@app.get("/api/whatsapp/blocked", dependencies=[Depends(_require_ops_key)])
async def api_whatsapp_blocked(limit: int = 5):
    from . import wasenders
    return {"senders": await wasenders.list_blocked(limit),
            "allowlist_active": bool(get_settings().allowlist)}


@app.post("/api/whatsapp/blocked/allow", dependencies=[Depends(_require_ops_key)])
async def api_whatsapp_allow_sender(body: dict):
    from . import wasenders
    try:
        return await wasenders.allow_sender(str((body or {}).get("sender") or ""))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/people/unmatched", dependencies=[Depends(_require_ops_key)])
async def api_unmatched_senders():
    from . import people
    return await people.unmatched_senders()


@app.post("/api/people/reresolve", dependencies=[Depends(_require_ops_key)])
async def api_reresolve():
    from . import people
    return {"reresolved_events": await people.reresolve_events()}


# ── Cases (ambient) ─────────────────────────────────────────────────────


@app.get("/api/cases", dependencies=[Depends(_require_ops_key)])
async def api_list_cases(status: str = "", limit: int = 100, sandbox: bool = False,
                         include_exploration: bool = False):
    pool = await db.get_pool()
    # Sandbox cases come from quarantined test threads (settings.test_threads) and
    # must never be mistaken for live client work: the filter is always applied,
    # so callers see real cases by default and sandbox ones only on request.
    # Quick 006: the shared exploration case is an internal log of info insights
    # (each warning or critical insight is its own case), hidden unless asked for.
    where = "WHERE c.sandbox = $2" + (" AND c.status = $3" if status else "")
    if not include_exploration:
        where += " AND c.origin IS DISTINCT FROM 'exploration'"
    args = [limit, sandbox] + ([status] if status else [])
    rows = await pool.fetch(
        # Quick 006: origin, links, seen_at and the fire count let the UIs show where a
        # case came from and whether the owner has seen the alert.
        f"""SELECT c.id, c.title, c.status, c.summary, c.close_reason, c.created_at, c.closed_at,
                   c.origin, c.watch_id, c.links, c.seen_at, c.last_activity_at,
                   (SELECT count(*) FROM case_events ce
                     WHERE ce.case_id = c.id AND ce.note = 'watch') AS fire_count,
                   count(*) FILTER (WHERE e.status='open')     AS open_exp,
                   count(*) FILTER (WHERE e.status='violated') AS violated_exp,
                   count(*) FILTER (WHERE e.status='satisfied') AS satisfied_exp
            FROM cases c LEFT JOIN expectations e ON e.case_id = c.id
            {where}
            GROUP BY c.id
            ORDER BY (c.status='open') DESC, (c.status='pending_ack') DESC, c.id DESC
            LIMIT $1""", *args)
    return [_case_row(r) for r in rows]


def _jsonb(v):
    """asyncpg returns jsonb as str: decode at the edge (Quick 006 case fields)."""
    import json as _json
    if isinstance(v, str):
        try:
            return _json.loads(v)
        except ValueError:
            return {}
    return v if v is not None else {}


def _case_row(r) -> dict:
    d = dict(r)
    if "links" in d:
        d["links"] = _jsonb(d["links"])
    return d


@app.get("/api/cases/{case_id}", dependencies=[Depends(_require_ops_key)])
async def api_case_detail(case_id: int):
    pool = await db.get_pool()
    case = await pool.fetchrow("SELECT * FROM cases WHERE id = $1", case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    participants = await pool.fetch(
        """SELECT p.id, p.name, cp.role FROM case_participants cp
           JOIN people p ON p.id = cp.person_id WHERE cp.case_id = $1""", case_id)
    expectations = await pool.fetch(
        # Quick 024: owed_by 'assistant' = a request the assistant owes (its reply
        # in the chat meets it); the UIs name the assistant instead of "unassigned".
        """SELECT e.id, e.description, e.status, e.due_at, e.reminder_count, e.satisfy_hint,
                  p.name AS owner, e.owed_by
           FROM expectations e LEFT JOIN people p ON p.id = e.owner_id
           WHERE e.case_id = $1 ORDER BY e.id""", case_id)
    timeline = await pool.fetch(
        # Quick 006: kind is the note label (watch, investigation, exploration, chat,
        # closing, manual, triage, ...) so the UIs can say what each entry is.
        """SELECT e.id, e.source, e.content, e.occurred_at, e.provenance,
                  COALESCE(p.name, e.actor_raw) AS actor, ce.note AS kind,
                  CASE WHEN ce.note IN ('exploration', 'chat', 'watch') THEN e.meta END AS meta
           FROM case_events ce JOIN events e ON e.id = ce.event_id
           LEFT JOIN people p ON p.id = e.actor_id
           WHERE ce.case_id = $1 ORDER BY e.occurred_at""", case_id)
    actions = await pool.fetch(
        """SELECT id, kind, payload, tier, status, created_at, executed_at
           FROM actions WHERE case_id = $1 ORDER BY id DESC""", case_id)
    from . import cases_util, chats
    out_case = _case_row(case)  # aliases ride along via SELECT *
    # Quick 024: where the case started, as people say it ("WhatsApp · Asha", the
    # group's name), never the raw number or JID; None when there is no chat.
    out_case["origin_chat_name"] = await chats.display_name(pool, out_case.get("origin_chat_jid"))
    # Quick 006: the watch an alert case links back to (None once it is deleted).
    watch = None
    if out_case.get("watch_id"):
        w = await pool.fetchrow(
            "SELECT id, title, kind, status, normal_streak FROM watches WHERE id = $1",
            out_case["watch_id"])
        watch = dict(w) if w else None
        if watch:
            from . import findingcases
            watch["recovery_checks"] = findingcases.recovery_checks()
    return {
        "case": out_case,
        "participants": [dict(r) for r in participants],
        "expectations": [dict(r) for r in expectations],
        "timeline": [{**dict(r), "meta": _jsonb(r["meta"]) if r["meta"] is not None else None}
                     for r in timeline],
        "actions": [dict(r) for r in actions],
        "facts": await cases_util.current_facts(pool, case_id),
        "watch": watch,
    }


@app.post("/api/cases/{case_id}/ack", dependencies=[Depends(_require_ops_key)])
async def api_ack_case(case_id: int):
    """Acknowledge a proposed close: pending_ack → closed (plan §1.4). Runs the
    same close path as auto-close, so notify/distill/chaining behave uniformly."""
    from . import triage
    pool = await db.get_pool()
    ok = await triage._close_case(pool, case_id, None, statuses=("pending_ack",))
    if not ok:
        raise HTTPException(status_code=409, detail="case is not pending acknowledgment")
    return {"closed": case_id}


@app.post("/api/cases/{case_id}/close", dependencies=[Depends(_require_ops_key)])
async def api_close_case(case_id: int):
    """Manually complete a case now (open or pending_ack → closed), without waiting
    for the 48h-quiet auto-close proposal. Same close path as ack/auto-close."""
    from . import findingcases, triage
    pool = await db.get_pool()
    if await findingcases.is_finding_case(pool, case_id):
        # Quick 006: same close path, after the case's own summary is written.
        ok = await findingcases.close_case(case_id, "closed by the owner", kind="completed",
                                           pool=pool)
    else:
        ok = await triage._close_case(pool, case_id, "closed manually",
                                      statuses=("open", "pending_ack"))
    if not ok:
        raise HTTPException(status_code=409, detail="case is not open")
    return {"closed": case_id}


@app.post("/api/cases/{case_id}/seen", dependencies=[Depends(_require_ops_key)])
async def api_case_seen(case_id: int):
    """Quick 006: the owner acknowledged an alert case. It stays open (it closes itself
    when the watch is back to normal) but no longer owes anything until the next fire."""
    from . import findingcases
    pool = await db.get_pool()
    if not await findingcases.mark_seen(pool, case_id):
        raise HTTPException(status_code=409, detail="case is not open")
    return {"seen": case_id}


@app.post("/api/cases/{case_id}/dismiss", dependencies=[Depends(_require_ops_key)])
async def api_dismiss_case(case_id: int):
    pool = await db.get_pool()
    await pool.execute(
        "UPDATE cases SET status='dismissed', closed_at=now() WHERE id=$1 AND status IN ('open','pending_ack')",
        case_id)
    return {"dismissed": case_id}


@app.post("/api/cases/{case_id}/note", dependencies=[Depends(_require_ops_key)])
async def api_add_case_note(case_id: int, body: dict):
    """A human tells the system something about a case. Goes through the one
    note write path (cases_util.add_case_note), so the note lands on the case
    timeline, the rolling summary and the reactive context exactly like a
    triage or investigation note — and triage never re-ingests it."""
    from . import cases_util
    pool = await db.get_pool()
    if await pool.fetchval("SELECT id FROM cases WHERE id = $1", case_id) is None:
        raise HTTPException(status_code=404, detail="case not found")
    text = str((body or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    async with pool.acquire() as conn:
        async with conn.transaction():
            eid = await cases_util.add_case_note(conn, case_id, text, kind="manual")
    return {"event_id": eid}


@app.post("/api/expectations/{exp_id}/dismiss", dependencies=[Depends(_require_ops_key)])
async def api_dismiss_expectation(exp_id: int):
    pool = await db.get_pool()
    await pool.execute(
        "UPDATE expectations SET status='dismissed', resolved_at=now() WHERE id=$1 AND status IN ('open','violated')",
        exp_id)
    return {"dismissed": exp_id}


# ── Skills ──────────────────────────────────────────────────────────────
# Procedure guidance the agent can pull in on demand: a vendored library at
# the repo root (skills_library/, read-only, may legitimately be absent) plus
# the `skills` table, whose body TEXT is the source of truth once installed.
# Installed is NOT enabled — a skill does nothing until a human flips
# `enabled`, and only enabled skills appear in the supervisor's index.
#
# Route order matters: the /api/skills/library routes are declared BEFORE
# /api/skills/{name} so 'library' resolves as the static path, not as a slug
# ('library' and 'install' are also reserved slugs, belt and braces).


def _reset_agents_for_skills() -> None:
    """Drop the cached agent graph after any skills mutation, fail-soft.

    Same invalidation pattern as configservice._invalidate: reset_agents()
    clears the built supervisor so the next run rebuilds with a fresh
    enabled-skills index. The agent-config PUT pattern (run()'s
    `configs != _built_with` comparison in agents/graph.py) would NOT work
    here — skills live in their own table and are invisible to that
    comparison, so a stale supervisor would keep serving the old index until
    an unrelated agent-config edit happened to force a rebuild.
    """
    try:
        from .agents import graph as _g
        _g.reset_agents()
    except Exception:
        # A failed reset must never fail the mutation; the DB row is already
        # correct and a restart rebuilds from it.
        log.exception("agent reset after skills change failed (restart will pick it up)")


async def _maybe_await(value):
    """Tolerate the skillservice seam being sync or async per helper."""
    if inspect.isawaitable(value):
        return await value
    return value


def _skill_meta(parsed) -> dict:
    """Frontmatter dict out of parse_skill_md(), whatever container it rides in."""
    if isinstance(parsed, dict):
        inner = parsed.get("meta")
        return inner if isinstance(inner, dict) else parsed
    if isinstance(parsed, (tuple, list)):
        for part in parsed:
            if isinstance(part, dict):
                return part
    return {}


def _valid_slug(skillservice, slug: str) -> bool:
    """True when validate_slug accepts it — whether it returns falsy or raises."""
    try:
        return skillservice.validate_slug(slug) is not False
    except Exception:
        return False


@app.get("/api/skills/library", dependencies=[Depends(_require_ops_key)])
async def api_skills_library(q: str = ""):
    """Search the vendored library. `library_available: false` (not an error)
    when skills_library/ is absent — client VMs may ship without it."""
    from . import skillservice
    if not await _maybe_await(skillservice.library_available()):
        return {"library_available": False, "skills": []}
    entries = await _maybe_await(skillservice.search_library(q))
    return {"library_available": True, "skills": entries or []}


@app.get("/api/skills/library/{source}/{name}", dependencies=[Depends(_require_ops_key)])
async def api_skills_library_entry(source: str, name: str):
    """One library entry with its SKILL.md body, for preview before install."""
    from . import skillservice
    entry = await _maybe_await(skillservice.library_entry(f"{source}/{name}"))
    if not entry:
        raise HTTPException(status_code=404, detail="unknown library skill")
    body = await _maybe_await(skillservice.library_skill_body(entry))
    if body is None:
        raise HTTPException(status_code=404, detail="library skill has no readable SKILL.md")
    return {**dict(entry), "body": body}


@app.get("/api/skills", dependencies=[Depends(_require_ops_key)])
async def api_list_skills():
    """Installed skills WITHOUT body — the dashboard polls this every 5s and
    bodies run to 64KB each; fetch one skill for the full text."""
    pool = await db.get_pool()
    rows = await pool.fetch(
        """SELECT id, name, description, source, origin_ref, license, compat,
                  enabled, scope, created_at, updated_at
           FROM skills ORDER BY name""")
    return [dict(r) for r in rows]


@app.get("/api/skills/{name}", dependencies=[Depends(_require_ops_key)])
async def api_skill_detail(name: str):
    pool = await db.get_pool()
    row = await pool.fetchrow("SELECT * FROM skills WHERE name = $1", name)
    if not row:
        raise HTTPException(status_code=404, detail="skill not found")
    return dict(row)


@app.post("/api/skills/install", dependencies=[Depends(_require_ops_key)])
async def api_install_skill(body: dict):
    """Copy a library skill's SKILL.md into the skills table (enabled=FALSE:
    installing is not consent — a human enables it separately)."""
    import asyncpg

    from . import skillservice
    entry_id = str((body or {}).get("id") or "").strip()
    entry = await _maybe_await(skillservice.library_entry(entry_id)) if entry_id else None
    if not entry:
        raise HTTPException(status_code=404, detail="unknown library skill")
    text = await _maybe_await(skillservice.library_skill_body(entry))
    # Empty/whitespace is as unusable as missing: use_skill would return the
    # framing header alone. Same 404 as unreadable (create requires non-empty too).
    if text is None or not text.strip():
        raise HTTPException(status_code=404, detail="library skill has no readable SKILL.md")
    max_body = getattr(skillservice, "MAX_BODY", 65536)
    if len(text.encode("utf-8", errors="replace")) > max_body:
        raise HTTPException(status_code=422, detail=f"SKILL.md exceeds {max_body} bytes")

    # Slug comes from the MANIFEST name (the library's identity for the skill),
    # not the frontmatter, which is untrusted text inside the file.
    slug = str(entry.get("name") or "").strip()
    if not _valid_slug(skillservice, slug):
        raise HTTPException(
            status_code=422,
            detail=f"bad skill slug {slug!r} — lowercase [a-z0-9-], max 64, not a reserved name")

    meta: dict = {}
    try:
        meta = _skill_meta(await _maybe_await(skillservice.parse_skill_md(text)))
    except Exception:
        pass  # frontmatter is optional; the manifest entry carries the metadata
    description = str(meta.get("description") or entry.get("description") or "").strip()
    license_ = str(meta.get("license") or entry.get("license") or "").strip() or None
    # 'general' fallback, not None: compat is NOT NULL and an explicit NULL in
    # the INSERT bypasses the column DEFAULT (found live — fake-pool tests
    # cannot see real constraints).
    compat = str(entry.get("compat") or "").strip() or "general"
    source = str(entry.get("source") or entry_id.split("/", 1)[0]).strip()

    pool = await db.get_pool()
    try:
        row = await pool.fetchrow(
            """INSERT INTO skills (name, description, source, origin_ref, license, compat, body, enabled)
               VALUES ($1, $2, $3, $4, $5, $6, $7, FALSE)
               RETURNING id, name, source, enabled""",
            slug, description, source, entry_id, license_, compat, text)
    except asyncpg.UniqueViolationError:
        existing = await pool.fetchrow("SELECT source FROM skills WHERE name = $1", slug)
        holder = existing["source"] if existing else "unknown"
        raise HTTPException(
            status_code=409,
            detail=f"a skill named '{slug}' is already installed (source: {holder}) — delete it first")
    _reset_agents_for_skills()
    return dict(row)


# What the drafting model is told this assistant IS. The capabilities line
# keeps drafts honest (a skill can only steer existing tools); the refusal
# contract makes the model itself the first relevance blocker, with
# skillservice.relevance_check as the deterministic second.
_DRAFT_SYSTEM = (
    "You draft SKILL.md files for a WhatsApp-based business assistant deployed "
    "inside one company. The assistant answers questions with read-only SQL "
    "over the business database, extracts data from documents, generates "
    "styled PDF/DOCX/XLSX reports, runs pandas analyses in a sandbox, does "
    "web research, sends WhatsApp messages, runs scheduled playbooks "
    "(reminders, digests) and tracks cases (invoices, orders, obligations). "
    "A skill is a PROCEDURE the assistant consults — plain instructions; it "
    "cannot add tools, run installers, or promise capabilities beyond those.\n"
    "Output ONLY the SKILL.md text: YAML frontmatter with name (a lowercase "
    "slug) and description (one line: what it does AND when to use it), then "
    "a concise body (under 150 lines) of concrete steps, formats and rules.\n"
    "If the request is not relevant to such an assistant (coding, devops, "
    "IDE workflows, desktop automation), reply with exactly: "
    "NOT RELEVANT: <one-line reason>\n\n"
    "Skill-authoring guidance:\n")


async def _draft_call(system: str, goal: str) -> str:
    """One LLM call for skill drafting; isolated so tests can fake it."""
    from . import llmclient
    s = get_settings()
    from . import usage
    model = llmclient.chat_anthropic(s.supervisor_model, max_tokens=4096,
                                     api_key=s.anthropic_api_key or None,
                                     role="supervisor", settings=s)  # Phase 13 (GOLIVE-01)
    with usage.attribute(feature="other", agent="skill_draft"):  # Quick 005
        msg = await model.ainvoke([("system", system), ("user", goal)])
    c = msg.content
    if isinstance(c, list):
        c = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
    return str(c)


@app.post("/api/skills/draft", dependencies=[Depends(_require_ops_key)])
async def api_draft_skill(body: dict):
    """Draft a SKILL.md from a one-line goal, guided by the vendored
    anthropics/skill-creator skill. Returns a draft for the operator to edit
    and submit — nothing is installed by this route."""
    from . import skillservice
    goal = str((body or {}).get("goal") or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="goal required")
    ok, reason = skillservice.relevance_check(goal)
    if not ok:
        raise HTTPException(status_code=422, detail=f"blocked: {reason}")
    if not get_settings().anthropic_api_key:
        raise HTTPException(status_code=503, detail="no LLM key configured")

    try:
        text = await _draft_call(_DRAFT_SYSTEM + skillservice.creator_guidance(), goal)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"draft failed: {type(e).__name__}")

    text = text.strip()
    if text.upper().startswith("NOT RELEVANT"):
        raise HTTPException(status_code=422, detail=f"blocked: {text[:300]}")
    # Models sometimes wrap output in a markdown fence; unwrap before parsing.
    import re as _re
    if text.startswith("```"):
        text = _re.sub(r"^```[A-Za-z]*\n", "", text)
        text = _re.sub(r"\n```\s*$", "", text)
    ok, reason = skillservice.relevance_check(text)
    if not ok:
        raise HTTPException(status_code=422, detail=f"blocked: {reason}")
    parsed = _skill_meta(await _maybe_await(skillservice.parse_skill_md(text)))
    return {
        "name": str(parsed.get("name") or ""),
        "description": str(parsed.get("description") or ""),
        "body": text,
    }


@app.post("/api/skills", dependencies=[Depends(_require_ops_key)])
async def api_create_skill(body: dict):
    """A custom skill typed or pasted by the operator. Same rules as install:
    valid slug, capped body, born disabled."""
    import asyncpg

    from . import skillservice
    b = body or {}
    slug = str(b.get("name") or "").strip()
    text = str(b.get("body") or "")
    if not _valid_slug(skillservice, slug):
        raise HTTPException(
            status_code=422,
            detail=f"bad skill slug {slug!r} — lowercase [a-z0-9-], max 64, not a reserved name")
    if not text.strip():
        raise HTTPException(status_code=422, detail="body required")
    max_body = getattr(skillservice, "MAX_BODY", 65536)
    if len(text.encode("utf-8", errors="replace")) > max_body:
        raise HTTPException(status_code=422, detail=f"body exceeds {max_body} bytes")

    description = str(b.get("description") or "").strip()
    if not description:
        try:
            description = str(
                _skill_meta(await _maybe_await(skillservice.parse_skill_md(text))).get("description") or "").strip()
        except Exception:
            description = ""

    # Relevance blocker: created skills must serve THIS assistant's work.
    # Deterministic (skillservice.relevance_check) so a rejection is
    # explainable and testable, and works with no LLM configured.
    ok, reason = skillservice.relevance_check(f"{slug} {description} {text}")
    if not ok:
        raise HTTPException(status_code=422, detail=f"blocked: {reason}")

    # scope='triage' makes this an owner triage policy (custom-only by
    # construction here — this route only ever writes source='custom').
    scope = str(b.get("scope") or "agent").strip()
    if scope not in getattr(skillservice, "SCOPES", {"agent", "triage"}):
        raise HTTPException(status_code=422, detail=f"bad scope {scope!r} — agent | triage")

    pool = await db.get_pool()
    try:
        row = await pool.fetchrow(
            """INSERT INTO skills (name, description, source, origin_ref, license, compat, body, enabled, scope)
               VALUES ($1, $2, 'custom', NULL, NULL, 'general', $3, FALSE, $4)
               RETURNING id, name, source, enabled, scope""",
            slug, description, text, scope)
    except asyncpg.UniqueViolationError:
        existing = await pool.fetchrow("SELECT source FROM skills WHERE name = $1", slug)
        holder = existing["source"] if existing else "unknown"
        raise HTTPException(
            status_code=409,
            detail=f"a skill named '{slug}' is already installed (source: {holder}) — delete it first")
    _reset_agents_for_skills()
    return dict(row)


@app.patch("/api/skills/{name}", dependencies=[Depends(_require_ops_key)])
async def api_update_skill(name: str, body: dict):
    """Enable/disable a skill. Enabling is the consent step — it is what puts
    the skill in the supervisor's index."""
    from . import skillservice
    pool = await db.get_pool()
    row = await pool.fetchrow("SELECT id, enabled FROM skills WHERE name = $1", name)
    if not row:
        raise HTTPException(status_code=404, detail="skill not found")

    enabled = (body or {}).get("enabled")
    # bool("false") is True — a string here would ENABLE a skill the caller
    # meant to disable. Refuse anything that isn't a real JSON boolean.
    if enabled is not None and not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="enabled must be a JSON boolean")
    # None/omitted KEEPS the current value — the api_update_chat convention:
    # a partial update must never silently revoke what a human turned on.
    if enabled is None or bool(enabled) == row["enabled"]:
        return {"name": name, "enabled": row["enabled"]}

    if enabled:
        max_enabled = getattr(skillservice, "MAX_ENABLED", 40)
        count = await pool.fetchval("SELECT count(*) FROM skills WHERE enabled")
        if count >= max_enabled:
            # 422, never silent truncation: the operator chooses what to drop.
            raise HTTPException(
                status_code=422,
                detail=f"{max_enabled} skills are already enabled — disable one first")

    await pool.execute(
        "UPDATE skills SET enabled = $2, updated_at = now() WHERE name = $1",
        name, bool(enabled))
    _reset_agents_for_skills()
    return {"name": name, "enabled": bool(enabled)}


@app.delete("/api/skills/{name}", dependencies=[Depends(_require_ops_key)])
async def api_delete_skill(name: str):
    pool = await db.get_pool()
    deleted = await pool.fetchval("DELETE FROM skills WHERE name = $1 RETURNING id", name)
    if deleted is None:
        raise HTTPException(status_code=404, detail="skill not found")
    _reset_agents_for_skills()
    return {"deleted": name}


# ── Phase 3: Dashboards ops API (routes in dashboards_api.py) ──────
from . import dashboards_api  # noqa: E402
app.include_router(dashboards_api.router, dependencies=[Depends(_require_ops_key)])
# ── end Phase 3 ──
# ── Phase 4: Dashboard serving origin (routes in dashboards_serve.py) ──
from . import dashboards_serve  # noqa: E402
app.include_router(dashboards_serve.router)  # no ops key: the view/preview token is the gate
# ── end Phase 4 ──

# ── Actions / approvals (ambient) ───────────────────────────────────────


_ACTION_KIND_RE = re.compile(r"^[a-z_]{1,64}$")  # Phase 8: WR-01


@app.get("/api/actions", dependencies=[Depends(_require_ops_key)])
async def api_list_actions(status: str = "proposed", limit: int = 100,
                           kind: str | None = None,
                           exclude_kind: str | None = None):  # Phase 8: WR-01
    from . import actionsvc
    for k in (kind, exclude_kind):  # Phase 8: WR-01
        if k is not None and not _ACTION_KIND_RE.match(k):  # Phase 8: WR-01
            raise HTTPException(422, "kind must be 1-64 lowercase letters or underscores")
    return await actionsvc.list_actions(status=status, limit=limit,
                                        kind=kind, exclude_kind=exclude_kind)  # Phase 8: WR-01


@app.post("/api/actions/bulk-approve", dependencies=[Depends(_require_ops_key)])
async def api_bulk_approve(body: dict):
    """Quick 001: approve many proposals server-side. Same per-item path as single approve."""
    from . import actionsvc
    ids = body.get("ids") if isinstance(body, dict) else None
    if (not isinstance(ids, list) or not ids or len(ids) > actionsvc.BULK_MAX
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        raise HTTPException(422, f"ids must be a list of 1-{actionsvc.BULK_MAX} action ids")
    return await actionsvc.approve_many(ids)


@app.post("/api/actions/{action_id}/approve", dependencies=[Depends(_require_ops_key)])
async def api_approve_action(action_id: int):
    from . import actionsvc
    res = await actionsvc.approve(action_id)
    if "error" in res:
        raise HTTPException(status_code=409, detail=res["error"])
    return res


@app.post("/api/actions/{action_id}/reject", dependencies=[Depends(_require_ops_key)])
async def api_reject_action(action_id: int):
    from . import actionsvc
    res = await actionsvc.reject(action_id)
    if "error" in res:
        raise HTTPException(status_code=409, detail=res["error"])
    return res


# ── Phase 2: context store routes (read-only) ──
from . import contextapi  # noqa: E402
app.include_router(contextapi.router, dependencies=[Depends(_require_ops_key)])

# ── Phase 7: insights routes (read-only) ──
from . import insights_api  # noqa: E402
app.include_router(insights_api.router, dependencies=[Depends(_require_ops_key)])

# ── Phase 8: Intelligence Corner operator UI plumbing ──
from . import askapi, ownerapi  # noqa: E402
app.include_router(askapi.router, dependencies=[Depends(_require_ops_key)])
app.include_router(ownerapi.router, dependencies=[Depends(_require_ops_key)])
# ── end Phase 8 ──

# ── Phase 13.1: forecasts API (routes in forecasts_api.py) ──
from . import forecasts_api  # noqa: E402
app.include_router(forecasts_api.router, dependencies=[Depends(_require_ops_key)])
# ── end Phase 13.1 ──

# ── Phase 11: health alerts API (GOLIVE-05) ──
from . import healthapi  # noqa: E402
app.include_router(healthapi.router, dependencies=[Depends(_require_ops_key)])


def _mount_test_hooks(app_, enabled: bool) -> None:
    """Quick 029 F6: the loop-stop hook exists only in test mode (IC_TEST_HOOKS=1)."""
    if not enabled:
        return
    app_.include_router(healthapi.test_router, dependencies=[Depends(_require_ops_key)])
    logging.getLogger("main").warning(
        "test hooks mounted (IC_TEST_HOOKS=1): POST /api/test-stop-loop/{name} "
        "can stop loops until restart")


_mount_test_hooks(app, healthapi.test_hooks_enabled())
# ── end Phase 11 ──

# ── Quick 005: usage ledger and run traces (routes in usageapi.py) ──
from . import usageapi  # noqa: E402
app.include_router(usageapi.router, dependencies=[Depends(_require_ops_key)])
# ── end Quick 005 ──

# ── Quick 008: Runs control center (routes in runsapi.py) ──
from . import runsapi  # noqa: E402
app.include_router(runsapi.router, dependencies=[Depends(_require_ops_key)])
# ── end Quick 008 ──

# ── Phase 16: model routes (read-only) ──
from . import modelapi  # noqa: E402
app.include_router(modelapi.router, dependencies=[Depends(_require_ops_key)])
# ── end Phase 16 ──
# ── Phase 15 (SRC-02, SRC-10): sources and credentials routes ──
from . import sourcesapi  # noqa: E402
app.include_router(sourcesapi.router, dependencies=[Depends(_require_ops_key)])
# ── End Phase 15 ──


@app.get("/api/playbooks", dependencies=[Depends(_require_ops_key)])
async def api_list_playbooks():
    pool = await db.get_pool()
    rows = await pool.fetch(
        """SELECT p.id, p.name, p.trigger, p.goal, p.tool_grants, p.autonomy_tier, p.status,
                  p.proposed_by, p.approved_by, p.created_at,
                  lr.id AS last_run_id, lr.status AS last_run_status,
                  lr.error AS last_run_error, lr.ended_at AS last_run_ended_at
           FROM playbooks p
           LEFT JOIN LATERAL (SELECT id, status, error, ended_at FROM task_runs
                               WHERE kind = 'playbook' AND playbook_id = p.id
                               ORDER BY id DESC LIMIT 1) lr ON true
           ORDER BY p.id""")
    out = []
    for r in rows:  # Quick 023: the latest run, so the page can show Run now busy / failed
        d = dict(r)
        run_id = d.pop("last_run_id")
        run = {"id": run_id, "status": d.pop("last_run_status"),
               "error": d.pop("last_run_error"), "ended_at": d.pop("last_run_ended_at")}
        d["last_run"] = run if run_id is not None else None
        out.append(d)
    return out


@app.patch("/api/playbooks/{playbook_id}", dependencies=[Depends(_require_ops_key)])
async def api_update_playbook(playbook_id: int, body: dict):
    """Activate/archive a playbook, or approve a system-proposed draft."""
    status = body.get("status")
    if status not in ("draft", "active", "archived"):
        raise HTTPException(status_code=422, detail="status must be draft|active|archived")
    pool = await db.get_pool()
    approver = "human" if status == "active" else None
    await pool.execute(
        "UPDATE playbooks SET status = $2, approved_by = COALESCE($3, approved_by) WHERE id = $1",
        playbook_id, status, approver)
    return {"updated": playbook_id, "status": status}


@app.post("/api/playbooks/{playbook_id}/run", dependencies=[Depends(_require_ops_key)])
async def api_run_playbook_now(playbook_id: int):
    """Quick 023: queue one run of a scheduled playbook now (the "Run now" button).
    The same queue and worker as the nightly cron; nothing else changes.
    404 unknown | 409 archived, not active, or a run already queued/running
    (detail.task_run_id is that run) | 422 not a scheduled (cron) playbook, since
    event playbooks need a case | 423 the daily AI budget is used up (resets_at
    says when). 200 {task_run_id}."""
    import json as _json

    from . import playbooks, usage
    pool = await db.get_pool()
    pb = await pool.fetchrow("SELECT id, name, status, trigger FROM playbooks WHERE id = $1",
                             playbook_id)
    if pb is None:
        raise HTTPException(status_code=404, detail={"message": "Playbook not found."})
    if pb["status"] == "archived":
        raise HTTPException(status_code=409, detail={
            "message": "This playbook is archived. Restore and activate it first."})
    if pb["status"] != "active":
        raise HTTPException(status_code=409, detail={"message": "Activate this playbook first."})
    trig = pb["trigger"]
    trig = _json.loads(trig) if isinstance(trig, str) else (trig or {})
    if trig.get("type") != "cron":
        raise HTTPException(status_code=422, detail={
            "message": "This playbook runs when something happens on a case, so it cannot be "
                       "run on its own."})
    reason = await usage.background_pause_reason()
    if reason:
        st = await usage.budget_state(pool)
        raise HTTPException(status_code=423, detail={
            "message": reason, "resets_at": st.get("resets_at")})
    run_id, created = await playbooks.enqueue_playbook_run_once(playbook_id)
    if not created:
        raise HTTPException(status_code=409, detail={
            "message": "A run of this playbook is already queued or running.",
            "task_run_id": run_id})
    return {"task_run_id": run_id}


# ── DB poller sources (ambient DB-awareness watch-list) ─────────────────


@app.get("/api/db-sources", dependencies=[Depends(_require_ops_key)])
async def api_get_db_sources():
    from .connectors.db_poller import get_sources
    return await get_sources()


@app.post("/api/db-sources/propose", dependencies=[Depends(_require_ops_key)])
async def api_propose_db_sources():
    """Read the business-context knowledge base and propose tables to watch for new
    rows (with an inferred watermark column). Human approves via PUT below."""
    from .connectors.db_poller import propose_sources
    return {"proposals": await propose_sources()}


@app.put("/api/db-sources", dependencies=[Depends(_require_ops_key)])
async def api_set_db_sources(body: dict):
    from .connectors.db_poller import set_sources
    saved = await set_sources(body.get("sources", []))
    return {"saved": saved}


@app.post("/api/db-sources/add", dependencies=[Depends(_require_ops_key)])
async def api_add_db_source(body: dict):
    """Manually add one table by name; auto-detects the watermark or returns the
    table's columns to choose from."""
    from .connectors.db_poller import add_source
    return await add_source(body.get("table", ""), body.get("watermark_column"))


@app.get("/api/db-poll-seconds", dependencies=[Depends(_require_ops_key)])
async def api_get_db_poll_seconds():
    from .connectors.db_poller import get_poll_seconds
    return {"seconds": await get_poll_seconds()}


@app.put("/api/db-poll-seconds", dependencies=[Depends(_require_ops_key)])
async def api_set_db_poll_seconds(body: dict):
    """Live-set the DB poll interval (no restart)."""
    from .connectors.db_poller import set_poll_seconds
    try:
        return {"seconds": await set_poll_seconds(int(body.get("seconds", 60)))}
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="seconds must be a number")


@app.post("/api/test-event", dependencies=[Depends(_require_ops_key)])
async def test_event(body: dict):
    """Inject a synthetic event straight into ingestion (mirrors /api/test-message).
    Body: {source?, content, actor_phone?|actor_email?|actor_alias?, thread_ref?,
           occurred_at?, external_id?}. Drives triage without a live channel."""
    from datetime import datetime, timezone

    from .connectors import RawEvent, ingest
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="content required")
    occurred = body.get("occurred_at")
    try:
        when = (datetime.fromisoformat(str(occurred).replace("Z", "+00:00"))
                if occurred else datetime.now(timezone.utc))
    except ValueError:
        when = datetime.now(timezone.utc)
    ext = body.get("external_id") or f"test-{int(datetime.now(timezone.utc).timestamp()*1000)}"
    eid = await ingest(RawEvent(
        source=body.get("source", "whatsapp"),
        external_id=str(ext),
        content=content,
        occurred_at=when,
        actor_phone=body.get("actor_phone"),
        actor_email=body.get("actor_email"),
        actor_alias=body.get("actor_alias"),
        actor_raw=body.get("actor_phone") or body.get("actor_email") or body.get("actor_alias"),
        thread_ref=body.get("thread_ref"),
    ))
    return {"event_id": eid, "duplicate": eid is None}


@app.post("/api/test-expire-expectations", dependencies=[Depends(_require_ops_key)])
async def test_expire_expectations(body: dict):
    """Test hook: push open expectations' due_at into the past so the scheduler
    treats them as violated on its next sweep. Scope to a case with case_id."""
    pool = await db.get_pool()
    case_id = body.get("case_id")
    if case_id:
        n = await pool.fetchval(
            """WITH u AS (UPDATE expectations SET due_at = now() - interval '1 minute'
               WHERE case_id = $1 AND status = 'open' RETURNING 1) SELECT count(*) FROM u""",
            int(case_id))
    else:
        n = await pool.fetchval(
            """WITH u AS (UPDATE expectations SET due_at = now() - interval '1 minute'
               WHERE status = 'open' RETURNING 1) SELECT count(*) FROM u""")
    return {"expired": n or 0}


@app.post("/api/test-message", dependencies=[Depends(_require_ops_key)])
async def test_message(body: dict):
    """Drive the agent without WhatsApp (dashboard test box)."""
    settings = get_settings()
    fake = {
        "phone": body.get("phone", "test-local"),
        "message": body.get("message", ""),
        "messageId": None,
    }
    # Optional passthroughs so attachments and group context are drivable
    # without WhatsApp — the webhook reads exactly these keys.
    for key in ("document", "mediaBase64", "filename", "chatId", "isGroup",
                "mentioned", "name", "lid"):  # Quick 017: lid
        if key in body:
            fake[key] = body[key]

    class _Req:
        async def json(self):
            return fake

    return await whatsapp_webhook(_Req(), x_webhook_secret=settings.webhook_secret)
