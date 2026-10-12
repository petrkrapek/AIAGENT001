import pytest

from aiagent.config import SmtpConfig
from aiagent.tools import ToolError, build_registry
from conftest import call


def test_registry_contents(registry):
    names = {tool.name for tool in registry.tools}
    assert names == {"get_current_time", "add_task", "list_tasks", "update_task", "http_request",
                     "ares_search", "ares_company", "save_lead", "list_leads", "get_lead",
                     "update_lead", "save_draft"}
    assert registry.server_tools == []


def test_no_local_file_or_shell_tools(registry):
    for name in ("write_file", "read_file", "list_files", "run_command", "export_leads"):
        assert name not in registry


def test_web_and_email_tools_optional(config, repos):
    config.web_tools = True
    config.smtp = SmtpConfig(host="smtp.example.com", sender="me@example.com")
    config.send_mode = "approve"
    registry = build_registry(config, repos)
    assert "send_email" in registry
    assert [t["name"] for t in registry.server_tools] == ["web_search", "web_fetch"]


def test_api_tool_order_is_deterministic(registry):
    names = [t["name"] for t in registry.to_api()]
    assert names == sorted(names)


def test_tasks_lifecycle(registry, repos):
    call(registry, "add_task", title="Zavolat Alfě", due="2026-10-15")
    call(registry, "add_task", title="Follow-up Beta")
    first = next(iter(repos.tasks.tasks))
    assert f"{first} [todo] Zavolat Alfě (termín: 2026-10-15)" in call(registry, "list_tasks")
    call(registry, "update_task", id=first, status="done")
    open_tasks = call(registry, "list_tasks")
    assert "Zavolat" not in open_tasks and "Follow-up" in open_tasks
    assert "Zavolat" in call(registry, "list_tasks", status="done")
    with pytest.raises(ToolError):
        call(registry, "update_task", id="recNOPE", status="done")
    with pytest.raises(ToolError):
        call(registry, "update_task", id=first, status="hotovo")


def test_approval_rules(registry):
    http = registry.get("http_request")
    assert not http.requires_approval({"url": "https://x", "method": "GET"})
    assert http.requires_approval({"url": "https://x", "method": "post"})
    assert not registry.get("save_lead").requires_approval({})


def test_current_time(registry):
    assert "Europe/Prague" in call(registry, "get_current_time")
    with pytest.raises(ToolError):
        call(registry, "get_current_time", timezone="Mars/Olympus")
