"""Chat gating + onboarding — the parts that decide whether the assistant acts.

Being added to a group is not consent to act in it, so the default must be
silence. These are the pure-logic guards; the DB paths are exercised by the
end-to-end webhook checks.
"""

from app.memoryservice import case_group, chat_group
from app.worker import _IGNORE_WORDS


def test_chat_group_id_is_graphiti_safe():
    # Graphiti group_ids allow alphanumeric/underscore only; a jid has '@' and '.'.
    gid = chat_group("120363044123456789@g.us")
    assert gid == "chat_120363044123456789_g_us"
    assert all(ch.isalnum() or ch == "_" for ch in gid)


def test_chat_and_case_scopes_cannot_collide():
    assert chat_group("1203@g.us") != case_group(1203)


def test_chat_group_handles_empty_jid():
    assert chat_group("") == "chat_"


def test_ignore_words_are_lowercase_and_stripped():
    # _handle_chat_onboarding lowercases and strips " .!" before matching, so a
    # capitalised entry here would silently never match.
    for w in _IGNORE_WORDS:
        assert w == w.lower().strip(" .!")


def test_ignore_vocabulary_covers_the_obvious_refusals():
    for w in ("ignore", "no", "skip"):
        assert w in _IGNORE_WORDS


def test_set_status_accepts_a_purpose_only_edit():
    # Reported by the frontend: defaulting a missing status to 'pending' meant
    # renaming an active group's purpose silently silenced it. A partial update
    # must never revoke approval, so status is now optional and COALESCEd.
    import inspect

    from app import chats

    sig = inspect.signature(chats.set_status)
    assert sig.parameters["status"].default is None
    src = inspect.getsource(chats.set_status)
    assert "COALESCE($2, status)" in src


def test_onboarding_read_path_hides_abandoned_sessions():
    # reset() marks the old interview abandoned and the next send() starts a
    # fresh one — so returning the abandoned transcript replays a conversation
    # the model has no memory of.
    import inspect

    from app import onboarding

    src = inspect.getsource(onboarding.get_session)
    assert "status <> 'abandoned'" in src
    assert onboarding.get_session.__defaults__[-1] is False   # include_abandoned


def test_onboarding_rows_decode_jsonb_before_returning():
    # asyncpg hands jsonb back as a string; a caller rendering it directly gets
    # one chat bubble per character.
    from app import onboarding

    class _Row(dict):
        pass

    row = _Row(transcript='[{"role": "user", "content": "hi"}]', profile=None)
    decoded = onboarding._decoded(row)
    assert decoded["transcript"] == [{"role": "user", "content": "hi"}]
    assert decoded["profile"] is None
