"""Quick 029 F3: the supervisor prompt as built (editable prompt + never-stored layers)."""
from app.agents import graph


def composed_supervisor_prompt(monkeypatch, prompt=None, **sup) -> str:
    """Build the agents with fakes and return the supervisor's final system prompt.
    `prompt` is the saved supervisor prompt (default: the shipped DEFAULT_PROMPTS text)."""
    seen = {}

    def fake_cra(model, tool_list, prompt=None):
        if "SUPERVISOR-MARK" in (prompt or ""):
            seen["supervisor"] = prompt
        return object()

    async def no_log(*a, **k):
        return None

    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    monkeypatch.setattr(graph, "_make_model", lambda cfg, role="supervisor", **kw: object())
    monkeypatch.setattr(graph, "_log_tool", no_log)
    configs = {k: dict(graph.DEFAULT_CONFIGS[k]) for k in graph.DEFAULT_CONFIGS}
    configs["supervisor"]["prompt"] = "SUPERVISOR-MARK\n" + (
        graph.DEFAULT_PROMPTS["supervisor"] if prompt is None else prompt)
    configs["supervisor"].update(sup)
    try:
        graph._build(configs)
    finally:
        graph.reset_agents()
    return seen["supervisor"]
