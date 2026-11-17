"""Group threading — the identity rules that make a group reply land in the group.

thread_ref is load-bearing in two directions at once: inbound events and
self-recorded outbound sends must produce the SAME key, or echo detection and
the satisfaction sweep silently stop working for that conversation.
"""

from app.connectors.whatsapp_events import _thread_ref

GROUP = "120363044123456789@g.us"


def test_group_jid_survives_both_directions():
    # Inbound passes the jid as group_jid; the self-record side only ever sees
    # it as `phone`, because worker.py sends to thread_id. Both must converge.
    assert _thread_ref("anything", GROUP) == GROUP
    assert _thread_ref(GROUP) == GROUP


def test_group_jid_is_not_reduced_to_digits():
    # The bug this guards: normalize_phone strips the "@g.us" and returns bare
    # digits, so the outbound key could never match the inbound one.
    assert _thread_ref(GROUP) != "120363044123456789"


def test_direct_chat_still_normalizes():
    # A user jid is NOT a group jid and must keep collapsing to digits.
    assert _thread_ref("+91 9000000101") == _thread_ref("919000000101@s.whatsapp.net")
    assert _thread_ref("919000000101@s.whatsapp.net") == "919000000101"
