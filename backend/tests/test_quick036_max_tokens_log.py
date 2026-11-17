"""Quick 036 (item 6): llmclient logs the max_tokens it actually sends when the Claude 5
thinking floor raises the configured value. The floor itself is unchanged. No network."""

import logging
from types import SimpleNamespace

from app import llmclient


def test_floor_is_logged_once_with_the_configured_and_sent_values(caplog):
    llmclient._floor_logged.clear()
    with caplog.at_level(logging.INFO, logger="llmclient"):
        for _ in range(3):
            m = llmclient.chat_anthropic("claude-opus-5-5", max_tokens=8192, api_key="k",
                                         role="supervisor",
                                         settings=SimpleNamespace(supervisor_effort="medium"))
    assert m.max_tokens == llmclient.THINKING_MAX_TOKENS_FLOOR == 16000
    lines = [r.getMessage() for r in caplog.records if "max_tokens" in r.getMessage()]
    assert lines == ["max_tokens for supervisor on claude-opus-5-5: configured 8192, "
                     "sent 16000 (thinking shares max_tokens; floor 16000)"]
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="llmclient"):
        llmclient.chat_anthropic("claude-opus-5-5", max_tokens=20000, api_key="k",
                                 role="supervisor",
                                 settings=SimpleNamespace(supervisor_effort="medium"))
    assert not [r for r in caplog.records if "max_tokens" in r.getMessage()]
