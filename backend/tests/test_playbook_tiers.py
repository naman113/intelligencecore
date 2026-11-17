"""Phase 3 — autonomy tier enforcement + token budget (pure logic).

The tier matrix is the security boundary for ambient autonomy, so it gets direct
coverage independent of the LLM.
"""

import pytest

from app.playbook_tools import TIER_CAPABILITIES, tier_allows


def test_autonomous_message_can_send_and_propose():
    assert tier_allows("autonomous_message", "send_reminder")
    assert tier_allows("autonomous_message", "propose_action")
    assert tier_allows("autonomous_message", "escalate")


def test_draft_cannot_send_reminders():
    assert not tier_allows("draft", "send_reminder")
    assert not tier_allows("draft", "escalate")
    assert tier_allows("draft", "propose_action")        # drafts are its whole job
    assert tier_allows("draft", "notify_owner")


def test_notify_is_message_only_no_reminders_no_drafts():
    assert not tier_allows("notify", "send_reminder")
    assert not tier_allows("notify", "propose_action")
    assert tier_allows("notify", "escalate")
    assert tier_allows("notify", "notify_owner")


def test_only_autonomous_message_may_post_to_a_group():
    # A DM nudges one person; a group post is visible to everyone in the chat.
    # It is therefore granted no more widely than send_reminder — notably NOT to
    # `notify`, which may message the requester but must not address a group.
    assert tier_allows("autonomous_message", "post_to_group")
    assert not tier_allows("notify", "post_to_group")
    assert not tier_allows("draft", "post_to_group")


def test_post_to_group_is_not_a_backdoor_around_send_reminder():
    # Any tier that can post to a group can already message people directly —
    # so the new capability never widens what a tier can reach.
    for tier, caps in TIER_CAPABILITIES.items():
        if "post_to_group" in caps:
            assert "send_reminder" in caps, tier


def test_default_deny_unknown_or_missing_tier():
    assert not tier_allows(None, "send_reminder")
    assert not tier_allows("bogus", "notify_owner")
    assert not tier_allows("", "propose_action")
    assert not tier_allows(None, "post_to_group")


def test_every_registered_playbook_tool_is_in_the_tier_matrix():
    # A tool reachable by an agent but absent from TIER_CAPABILITIES would be
    # default-denied everywhere — silently dead rather than loudly wrong.
    from app.playbook_tools import PLAYBOOK_TOOL_REGISTRY

    granted = set().union(*TIER_CAPABILITIES.values())
    for name in PLAYBOOK_TOOL_REGISTRY:
        assert name in granted, f"{name} is registered but no tier grants it"


def test_every_tier_can_notify_the_operator():
    for tier in TIER_CAPABILITIES:
        assert tier_allows(tier, "notify_owner")


def test_token_budget_aborts_when_exceeded():
    from app.playbooks import BudgetExceeded, TokenBudget

    class _Resp:
        def __init__(self, n):
            self.llm_output = {"token_usage": {"total_tokens": n}}
            self.generations = []

    tb = TokenBudget(budget=100)
    tb.on_llm_end(_Resp(60))          # 60 total — under budget
    assert tb.used == 60
    with pytest.raises(BudgetExceeded):
        tb.on_llm_end(_Resp(60))      # 120 total — over budget → abort


def test_exploration_caps_draft_and_autonomous_message_only():
    for cap in ("record_insight", "propose_kpi"):
        assert cap in TIER_CAPABILITIES["draft"]
        assert cap in TIER_CAPABILITIES["autonomous_message"]
        assert cap not in TIER_CAPABILITIES["notify"]
