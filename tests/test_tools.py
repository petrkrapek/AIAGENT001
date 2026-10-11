import pytest

from aiagent.config import SmtpConfig
from aiagent.tools import ToolError, build_registry


def call(registry, tool, **kwargs):
    return registry.get(tool).handler(**kwargs)


def test_registry_contents(config):
    registry = build_registry(config)
    names = {tool.name for tool in registry.tools}
    assert {"get_current_time", "list_files", "read_file", "write_file", "run_command",
            "add_task", "list_tasks", "update_task", "http_request", "ares_search",
            "ares_company", "save_lead", "list_leads", "get_lead", "update_lead",
            "save_draft", "export_leads"} <= names
    assert "send_email" not in names  # SMTP not configured
    assert registry.server_tools == []


def test_web_and_email_tools_optional(config):
    config.web_tools = True
    config.smtp = SmtpConfig(host="smtp.example.com", sender="me@example.com")
    registry = build_registry(config)
    assert "send_email" in registry
    assert [t["name"] for t in registry.server_tools] == ["web_search", "web_fetch"]


def test_api_tool_order_is_deterministic(config):
    api = build_registry(config).to_api()
    names = [t["name"] for t in api]
    assert names == sorted(names)


def test_files_roundtrip(config):
    registry = build_registry(config)
    call(registry, "write_file", path="notes/a.txt", content="ahoj")
    call(registry, "write_file", path="notes/a.txt", content=" světe", append=True)
    assert call(registry, "read_file", path="notes/a.txt") == "ahoj světe"
    assert "notes/" in call(registry, "list_files")


def test_files_refuse_paths_outside_workspace(config):
    registry = build_registry(config)
    with pytest.raises(ToolError):
        call(registry, "read_file", path="../../etc/passwd")
    with pytest.raises(ToolError):
        call(registry, "write_file", path="/tmp/evil.txt", content="x")


def test_tasks_lifecycle(config):
    registry = build_registry(config)
    call(registry, "add_task", title="Zaplatit fakturu", due="2026-10-15")
    call(registry, "add_task", title="Zavolat účetní")
    assert "#1 [todo] Zaplatit fakturu (termín: 2026-10-15)" in call(registry, "list_tasks")
    call(registry, "update_task", id=1, status="done")
    open_tasks = call(registry, "list_tasks")
    assert "Zaplatit" not in open_tasks and "Zavolat" in open_tasks
    assert "Zaplatit" in call(registry, "list_tasks", status="done")
    with pytest.raises(ToolError):
        call(registry, "update_task", id=99, status="done")
    with pytest.raises(ToolError):
        call(registry, "update_task", id=1, status="hotovo")


def test_run_command(config):
    registry = build_registry(config)
    out = call(registry, "run_command", command="echo ahoj")
    assert "exit code: 0" in out and "ahoj" in out


def test_approval_rules(config):
    registry = build_registry(config)
    assert registry.get("write_file").requires_approval({})
    assert registry.get("run_command").requires_approval({})
    assert not registry.get("read_file").requires_approval({})
    http = registry.get("http_request")
    assert not http.requires_approval({"url": "https://x", "method": "GET"})
    assert http.requires_approval({"url": "https://x", "method": "post"})


def test_current_time(config):
    registry = build_registry(config)
    assert "Europe/Prague" in call(registry, "get_current_time")
    with pytest.raises(ToolError):
        call(registry, "get_current_time", timezone="Mars/Olympus")
