"""Onboarding interviews — the only place the system learns what it can't infer.

Everywhere else, the assistant works from evidence: rows it queried, messages it
was sent, documents it read. But it starts knowing nothing about the business it
was deployed into, and the connector only sees messages from now onwards — so
there is no history to mine for "what is this company" or "what is this group
for". That gap is what makes a fresh instance improvise.

So we ask. A dedicated interviewer talks to the primary user until it genuinely
understands, then writes what it learned into memory where every other agent
picks it up.

Two modes, one interviewer:
  business — once per deployment: the company, its people, its priorities.
  chat     — once per group: what it's for, who matters, what to track.

Design decisions worth keeping:

* **The completion signal is a tool call, not prose.** An interviewer that can
  end by writing "great, I think I have everything" will do so while still
  missing things, and nothing downstream can tell the difference. Emitting the
  structured profile is the only way to finish, so "done" is a fact rather than
  a judgement about phrasing.

* **Told vs inferred are separate fields.** The interviewer is asked not to
  assume; recording its inferences apart from the user's actual words is what
  makes that checkable rather than aspirational, and lets a human correct the
  guesses without discarding the facts.

* **Highest-capability model, high effort.** This runs a few dozen turns once,
  so quality dominates cost by orders of magnitude — the one place in this
  system where the expensive model is obviously correct. Thinking is on by
  default on Opus 5; effort is now set explicitly through `onboarding_effort`
  (default high) because Opus 5.5 defaults to medium. Phase 13 (GOLIVE-01).

* **A completed interview stays open.** More detail can be added at any time;
  the next profile save updates it in place and re-applies. `finalize` forces
  the save mid-conversation via structured output (output_config json_schema),
  with a strict tool + tool_choice auto fallback (Claude 5 models reject
  forced tool_choice and thinking disabled). It is the escape hatch for the one
  failure the tool-call design can't prevent: a model that *announces* it will
  save in prose and then never calls the tool.
"""

import copy
import json
import logging

from . import llmclient
from .config import get_settings
from .db import get_pool

log = logging.getLogger("onboarding")

MAX_TOKENS = 16000          # thinking and reply share this ceiling
TRANSCRIPT_LIMIT = 120      # turns kept in context; interviews don't run longer


# ── the interviewer ──────────────────────────────────────────────────────
#
# Written as constraints on HOW to interview rather than a script of questions.
# A fixed question list would be a form with extra steps: it can't follow up on
# a surprising answer, and it asks everyone the same things regardless of what
# they already said.

_INTERVIEWER = """\
You are conducting an onboarding interview. Your only job is to UNDERSTAND. You
are not solving anything, not advising, and not demonstrating expertise.

How to interview:
- ONE question at a time. A batch of questions is an interrogation; people
  answer the easiest one and the rest are lost.
- Ask open questions and let them talk. "Walk me through how that works" beats
  "is it X or Y?" — a binary question smuggles in your assumption about the
  answer space.
- Never suggest the answer inside the question. Not "so you probably invoice
  monthly?" but "how does billing work?". A leading question gets you your own
  guess reflected back, and you will then treat it as something they told you.
- Follow the surprising thread. If something they say doesn't fit what you
  assumed, ask about that rather than continuing your plan.
- Reflect back to confirm, in their words, not yours. Do not paraphrase their
  domain terms into generic business language — the specific words they use are
  themselves information.
- If an answer is vague, say what's still unclear and ask again. Vague answers
  are the ones that produce confident wrong behaviour later.
- Never fill a gap with a plausible assumption. If you don't know, you ask. If
  they don't know either, record that it is unknown.
- Be easy to talk to: warm, plain language, no jargon of your own, no numbered
  checklists fired at them. Brief responses — they should be doing most of the
  talking.

You have a tool to finish. Do NOT call it while anything material is still
unclear — an incomplete profile is worse than a longer conversation, because
everything downstream treats it as ground truth. Before calling it, ask
yourself what you would still be guessing about, and go ask about that instead.

When you do call it, separate what you were TOLD from what you INFERRED. Your
inferences may be wrong and a human needs to be able to see which is which.
"""

_BUSINESS_BRIEF = """\
You are establishing ground context for an assistant that has just been
deployed into this business and currently knows nothing about it.

Cover, in whatever order the conversation naturally takes:
- What the business does, and how it actually makes money.
- The teams and the people who matter — names, roles, who depends on whom.
- The recurring work: what happens weekly, monthly, at period end.
- What "urgent" means here, and what must never be missed.
- The vocabulary: terms, codes, and shorthand used internally.
- What this person wants the assistant to watch for or chase.

On your FIRST message only, say what you're doing and roughly how long it will
take, then ask your first question. Never introduce yourself again after that —
once you are underway, every message is a brief acknowledgement plus the next
question.
"""

_CHAT_BRIEF = """\
You are establishing what one specific WhatsApp group is for. The assistant has
just been added to it and can see messages only from now onwards — it has no
history to learn from.

The group is "{subject}".

Cover:
- What this group is for, and what actually gets discussed in it.
- Who is in it and which of them matter for the work.
- What the assistant should track: commitments, deadlines, deliverables.
- What is routine chatter it should stay out of.
- When it should speak up, beyond being directly @mentioned.

This is a shorter conversation than the full business interview — a handful of
questions. On your FIRST message only, name the group and ask what it's for;
after that, never re-introduce yourself.
"""

_BUSINESS_TOOL = {
    "name": "save_business_profile",
    "description": (
        "Record the finished business profile. Call this ONLY when nothing "
        "material is still unclear — everything here is treated as ground truth "
        "by every other agent in the system."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description":
                        "2-4 sentences: what this business does and how it works."},
            "facts_told": {"type": "array", "items": {"type": "string"},
                           "description": "Durable facts the user stated. Their words, not paraphrased."},
            "inferences": {"type": "array", "items": {"type": "string"},
                           "description": "What you concluded but were NOT explicitly told."},
            "people": {"type": "array", "description":
                       "People named, for the People registry.",
                       "items": {"type": "object", "properties": {
                           "name": {"type": "string"},
                           "role": {"type": "string"},
                           "notes": {"type": "string"}}}},
            "vocabulary": {"type": "array", "items": {"type": "string"},
                           "description": "Internal terms and what they mean."},
            "priorities": {"type": "array", "items": {"type": "string"},
                           "description": "What counts as urgent; what must never be missed."},
            "recurring_work": {"type": "array", "items": {"type": "string"},
                               "description": "Repeating obligations, for playbook drafts."},
            "persona": {"type": "string", "description":
                        "A system prompt describing what this assistant is, for THIS business. "
                        "Written in the second person, a short paragraph."},
            "open_questions": {"type": "array", "items": {"type": "string"},
                               "description": "Things still unknown, including ones the user could not answer."},
        },
        "required": ["summary", "facts_told", "persona"],
    },
}

_CHAT_TOOL = {
    "name": "save_chat_profile",
    "description": (
        "Record what this group is for. Call this ONLY when you understand the "
        "group well enough that the assistant could act in it sensibly."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "purpose": {"type": "string", "description":
                        "One or two sentences: what this group is for."},
            "facts_told": {"type": "array", "items": {"type": "string"}},
            "participants": {"type": "array", "items": {"type": "string"},
                             "description": "Who matters here and why."},
            "track": {"type": "array", "items": {"type": "string"},
                      "description": "What the assistant should watch for and follow up on."},
            "ignore": {"type": "array", "items": {"type": "string"},
                       "description": "Routine chatter to stay out of."},
            "speak_up_when": {"type": "array", "items": {"type": "string"},
                              "description": "Beyond an @mention, when it should say something."},
        },
        "required": ["purpose"],
    },
}


def _brief(kind: str, subject: str | None) -> str:
    if kind == "chat":
        return _INTERVIEWER + "\n\n" + _CHAT_BRIEF.format(subject=subject or "this group")
    return _INTERVIEWER + "\n\n" + _BUSINESS_BRIEF


def _tool(kind: str) -> dict:
    return _CHAT_TOOL if kind == "chat" else _BUSINESS_TOOL


# ── session state ────────────────────────────────────────────────────────

def _decoded(row) -> dict:
    """asyncpg returns jsonb as a STRING. Callers want Python objects — a caller
    that renders the raw value gets one chat bubble per character."""
    d = dict(row)
    for col, empty in (("transcript", []), ("profile", None)):
        v = d.get(col)
        d[col] = json.loads(v) if isinstance(v, str) else (v if v is not None else empty)
    return d


async def get_session(kind: str, subject_ref: str | None = None,
                      include_abandoned: bool = False) -> dict | None:
    """Latest interview for this subject.

    Abandoned sessions are excluded by default: `reset` marks the old one
    abandoned and the next `send` starts a fresh one, so returning it would
    replay a conversation the model has no memory of.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        f"""SELECT * FROM onboarding_sessions
             WHERE kind = $1 AND subject_ref IS NOT DISTINCT FROM $2
               {'' if include_abandoned else "AND status <> 'abandoned'"}
             ORDER BY id DESC LIMIT 1""", kind, subject_ref)
    return _decoded(row) if row else None


async def _start(kind: str, subject_ref: str | None) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO onboarding_sessions (kind, subject_ref) VALUES ($1, $2)
           RETURNING *""", kind, subject_ref)
    # _decoded, not dict: RETURNING * hands back jsonb as a string just like a
    # SELECT does, and a raw "[]" transcript is truthy AND iterable-by-character
    # — every downstream consumer breaks in a different way.
    return _decoded(row)


async def reset(kind: str, subject_ref: str | None = None) -> None:
    """Abandon any live interview so a fresh one can start. Completed sessions
    are left alone — they are the provenance of an applied profile."""
    pool = await get_pool()
    await pool.execute(
        """UPDATE onboarding_sessions SET status = 'abandoned', updated_at = now()
            WHERE kind = $1 AND subject_ref IS NOT DISTINCT FROM $2
              AND status = 'active'""", kind, subject_ref)


async def _save(session_id: int, transcript: list, profile: dict | None) -> None:
    pool = await get_pool()
    await pool.execute(
        """UPDATE onboarding_sessions
              SET transcript = $2::jsonb,
                  profile = COALESCE($3::jsonb, profile),
                  status = CASE WHEN $3 IS NULL THEN status ELSE 'complete' END,
                  updated_at = now()
            WHERE id = $1""",
        session_id, json.dumps(transcript), json.dumps(profile) if profile else None)


# ── the conversation ─────────────────────────────────────────────────────

async def send(kind: str, message: str | None = None,
               subject_ref: str | None = None) -> dict:
    """Advance the interview one turn.

    `message=None` opens the conversation (the interviewer speaks first, which
    is what makes it an interview rather than a blank prompt the user has to
    guess how to fill).
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        return {"error": "onboarding needs the Anthropic key (Setup page)"}

    session = await get_session(kind, subject_ref)
    # 'complete' continues the same session rather than starting a blank one:
    # the operator adding a fact a week later should extend what the model
    # already knows, and the next save updates the profile in place.
    if not session or session["status"] not in ("active", "complete"):
        session = await _start(kind, subject_ref)

    transcript = list(session["transcript"] or [])   # already decoded by _decoded()

    if message:
        transcript.append({"role": "user", "content": message})
    elif transcript:
        # Re-opening an interview already in progress: replay, don't restart.
        return {"reply": _last_assistant_text(transcript),
                "done": session["status"] == "complete",
                "profile": session.get("profile"),
                "transcript": transcript}

    subject = await _subject(kind, subject_ref)

    client = llmclient.anthropic_async(settings.anthropic_api_key, role="interview",
                                       feature="interview",  # Quick 005
                                       trace=f"interview:{session['id']}")

    try:
        msg = await client.messages.create(
            **llmclient.messages_kwargs(  # Phase 13 (GOLIVE-01)
                settings.onboarding_model, role="onboarding",
                max_tokens=MAX_TOKENS, settings=settings),
            system=_brief(kind, subject),
            tools=[_tool(kind)],
            messages=_for_api(transcript),
        )
    except Exception as e:
        log.exception("onboarding model call failed")
        return {"error": f"{type(e).__name__}: {e}"}

    if llmclient.is_refusal(msg):  # Phase 12 (CTX-09e)
        return {"reply": llmclient.REFUSAL_REPLY, "done": False,
                "transcript": transcript}

    reply_parts, profile = [], None
    for block in msg.content:
        if getattr(block, "type", "") == "text":
            reply_parts.append(block.text)
        elif getattr(block, "type", "") == "tool_use":
            profile = dict(block.input)

    reply = "\n".join(p for p in reply_parts if p.strip()).strip()
    if reply:
        transcript.append({"role": "assistant", "content": reply})

    if profile:
        applied = await _apply(kind, subject_ref, profile)
        await _save(session["id"], transcript, profile)
        return {"reply": reply, "done": True, "profile": profile,
                "applied": applied, "transcript": transcript}

    await _save(session["id"], transcript, None)
    return {"reply": reply, "done": False, "transcript": transcript}


async def _subject(kind: str, subject_ref: str | None) -> str | None:
    """Human name for the interview subject — a chat's WhatsApp group title."""
    if kind != "chat" or not subject_ref:
        return subject_ref
    try:
        from . import chats
        row = next((c for c in await chats.list_chats()
                    if c["jid"] == subject_ref), None)
        return (row or {}).get("subject") or subject_ref
    except Exception:
        log.debug("chat subject lookup failed", exc_info=True)
        return subject_ref


def _strict_schema(schema: dict) -> dict:
    """Deep copy with additionalProperties:false on every object node (required
    by structured output and strict tools). The input is not mutated."""
    out = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(out)
    return out


async def finalize(kind: str, subject_ref: str | None = None) -> dict:
    """Force the save NOW, whatever the interviewer thinks is still missing.

    Phase 12 (CTX-09e): structured output (output_config json_schema) makes the
    profile unavoidable without forced tool_choice or thinking disabled, both of
    which Claude 5 models reject. If structured output is unsupported, falls
    back to a strict tool with tool_choice auto and an explicit instruction.
    Works on active and completed sessions alike; anything not covered lands in
    open_questions rather than being invented.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        return {"error": "onboarding needs the Anthropic key (Setup page)"}

    session = await get_session(kind, subject_ref)
    transcript = list(session["transcript"] or []) if session else []
    if not transcript:
        return {"error": "nothing to save yet — the interview hasn't started"}

    transcript.append({"role": "user", "content":
                       "Save the profile now with everything we've covered. "
                       "Record anything we didn't get to as open questions."})

    client = llmclient.anthropic_async(settings.anthropic_api_key, role="interview",
                                       feature="interview",  # Quick 005
                                       trace=f"interview:{session['id']}" if session else None)
    tool = _tool(kind)
    schema = _strict_schema(tool["input_schema"])
    system = _brief(kind, await _subject(kind, subject_ref))
    profile = None
    try:
        try:
            msg = await client.messages.create(
                **llmclient.messages_kwargs(  # Phase 13 (GOLIVE-01)
                    settings.onboarding_model, role="onboarding",
                    max_tokens=MAX_TOKENS, settings=settings,
                    output_config={"format": {"type": "json_schema", "schema": schema}}),
                system=system,
                messages=_for_api(transcript),
            )
            if llmclient.is_refusal(msg):
                return {"error": "the model declined to save the profile"}
            text = "".join(b.text for b in msg.content
                           if getattr(b, "type", "") == "text")
            try:
                profile = json.loads(text)
            except (ValueError, TypeError):
                return {"error": "the model returned an invalid profile"}
            if not isinstance(profile, dict):
                return {"error": "the model returned an invalid profile"}
        except Exception as e:
            if "output_config" not in str(e) and "format" not in str(e):
                raise
            log.warning("structured output unsupported, using strict tool: %s", e)
            msgs = _for_api(transcript) + [{"role": "user", "content":
                                            f"Call {tool['name']} now with everything we've covered."}]
            msg = await client.messages.create(
                **llmclient.messages_kwargs(  # Phase 13 (GOLIVE-01)
                    settings.onboarding_model, role="onboarding",
                    max_tokens=MAX_TOKENS, settings=settings,
                    tool_choice={"type": "auto"}),
                system=system,
                tools=[{**tool, "strict": True, "input_schema": schema}],
                messages=msgs,
            )
            if llmclient.is_refusal(msg):
                return {"error": "the model declined to save the profile"}
            profile = next((dict(b.input) for b in msg.content
                            if getattr(b, "type", "") == "tool_use"), None)
            if not profile:
                return {"error": "the model returned no profile"}
    except Exception as e:
        log.exception("onboarding finalize failed")
        return {"error": f"{type(e).__name__}: {e}"}

    applied = await _apply(kind, subject_ref, profile)
    await _save(session["id"], transcript, profile)
    return {"reply": "", "done": True, "profile": profile,
            "applied": applied, "transcript": transcript}


def _for_api(transcript: list) -> list:
    """Trim to the recent window and guarantee the array opens on a user turn —
    the API rejects a leading assistant message, which is exactly what a naive
    tail-slice of an interview produces."""
    turns = transcript[-TRANSCRIPT_LIMIT:]
    while turns and turns[0]["role"] != "user":
        turns = turns[1:]
    return turns or [{"role": "user", "content": "Let's begin."}]


def _last_assistant_text(transcript: list) -> str:
    for turn in reversed(transcript):
        if turn["role"] == "assistant":
            return turn["content"]
    return ""


# ── applying what was learned ────────────────────────────────────────────

async def _apply(kind: str, subject_ref: str | None, profile: dict) -> dict:
    """Write the profile where the rest of the system will find it.

    Every step is best-effort and reported individually: a failure to draft a
    playbook must not lose the business facts, and the operator needs to see
    which parts landed rather than a single opaque success.
    """
    if kind == "chat":
        return await _apply_chat(subject_ref, profile)
    return await _apply_business(profile)


def business_episode_text(profile: dict) -> tuple[str, int]:
    """The graph episode a completed business interview becomes, and how many
    facts it carries. Shared with the graph rebuild (Quick 014)."""
    facts = list(profile.get("facts_told") or [])
    for extra in ("vocabulary", "priorities"):
        facts += list(profile.get(extra) or [])
    text = (profile.get("summary") or "") + "\n" + "\n".join(f"- {f}" for f in facts)
    return text, len(facts)


def chat_episode_text(profile: dict) -> str:
    """The graph episode a completed chat interview becomes (chat scope).
    Shared with the graph rebuild (Quick 014)."""
    lines = [(profile.get("purpose") or "").strip()]
    for key, label in (("participants", "Who matters"), ("track", "Track"),
                       ("ignore", "Ignore"), ("speak_up_when", "Speak up when")):
        for item in profile.get(key) or []:
            lines.append(f"{label}: {item}")
    return "\n".join(l for l in lines if l)


async def _apply_business(profile: dict) -> dict:
    from . import memoryservice
    applied = {}

    text, n_facts = business_episode_text(profile)
    try:
        await memoryservice.memory_graph.add_episode(
            text, source="onboarding",
            group_id=memoryservice.GLOBAL_GROUP)
        applied["memory"] = f"{n_facts} fact(s) written to global scope"
    except Exception:
        log.exception("memory seed failed")
        applied["memory"] = "failed (graph unavailable)"

    # Phase 12 (CTX-09c): the persona stays in onboarding_sessions.profile (saved by
    # _save) and the supervisor appends it to its default prompt. Writing it into
    # agent_configs replaced the HARD RULES and the Agents PUT wiped it anyway.
    persona = (profile.get("persona") or "").strip()
    if persona:
        from .agents import graph
        graph.reset_agents()              # applies to the next task, no restart
        applied["persona"] = "kept alongside the default supervisor instructions"

    people = [p for p in (profile.get("people") or []) if p.get("name")]
    applied["people"] = (f"{len(people)} proposed — add numbers on the People tab"
                         if people else "none named")
    applied["playbook_drafts"] = _describe_recurring(profile)
    applied["open_questions"] = profile.get("open_questions") or []
    return applied


def _describe_recurring(profile: dict) -> str:
    items = profile.get("recurring_work") or []
    # Deliberately NOT auto-created: a playbook is a thing that messages people
    # on its own, and nothing in this system activates one without a human.
    return (f"{len(items)} recurring obligation(s) captured — review on Playbooks"
            if items else "none captured")


async def _apply_chat(jid: str | None, profile: dict) -> dict:
    if not jid:
        return {"error": "no chat jid"}
    from . import chats, memoryservice
    applied = {}

    purpose = (profile.get("purpose") or "").strip()
    try:
        await chats.set_status(jid, "active", purpose=purpose)
        applied["chat"] = "activated"
    except Exception:
        log.exception("chat activation failed")
        applied["chat"] = "failed"

    try:
        await memoryservice.memory_graph.add_episode(
            chat_episode_text(profile), source="chat_onboarding",
            group_id=memoryservice.chat_group(jid))
        applied["memory"] = "written to this group's scope"
    except Exception:
        log.exception("chat memory seed failed")
        applied["memory"] = "failed (graph unavailable)"
    return applied
