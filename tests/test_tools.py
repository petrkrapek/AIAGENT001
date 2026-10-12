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
    assert http.requires_approval({"url": "https://x", "method": "GET"})
    assert http.requires_approval({"url": "https://x", "method": "post"})
    assert not registry.get("save_lead").requires_approval({})


def test_strict_tool_definitions(registry):
    api = {t["name"]: t for t in registry.to_api()}
    assert api["http_request"].get("strict") is None
    assert all(t.get("strict") for name, t in api.items() if name != "http_request")
    for name, tool in api.items():
        for prop, spec in tool["input_schema"]["properties"].items():
            assert spec.get("description") or spec.get("enum"), f"{name}.{prop} bez popisu"


def test_http_request_blocks_internal_addresses(registry):
    for url in ("http://127.0.0.1:8080/admin", "http://169.254.169.254/latest/meta-data",
                "http://localhost/", "file:///etc/passwd", "ftp://example.com/x"):
        with pytest.raises(ToolError):
            call(registry, "http_request", url=url)


def test_http_request_refuses_redirect_to_internal(registry):
    from aiagent.tools import web
    with pytest.raises(ToolError):
        web._SafeRedirect().redirect_request(None, None, 302, "Found", {}, "http://10.0.0.1/")


def test_console_output_is_sanitized():
    from aiagent.cli import _short, safe
    assert "\x1b" not in safe("ok\x1b[2K\rto: jiny@x.cz")
    assert "\x1b" not in _short({"body": "a\x1b[1A"})
    assert _short({"headers": {"Authorization": "Bearer tajne"}}) == '{"headers": {"Authorization": "***"}}'


def test_current_time(registry):
    assert "Europe/Prague" in call(registry, "get_current_time")
    with pytest.raises(ToolError):
        call(registry, "get_current_time", timezone="Mars/Olympus")


def test_http_request_success_and_size_cap(registry, monkeypatch):
    from aiagent.tools import web

    class Resp:
        status = 200

        def read(self, limit):
            assert limit == web.MAX_RESPONSE_BYTES
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    sent = {}

    def fake_open(request, timeout):
        sent["method"], sent["data"] = request.get_method(), request.data
        return Resp()

    monkeypatch.setattr(web, "check_public_url", lambda url: None)
    monkeypatch.setattr(web._opener, "open", fake_open)
    out = call(registry, "http_request", url="https://hook.example/x", method="post", json_body={"a": 1})
    assert out.startswith("HTTP 200") and sent == {"method": "POST", "data": b'{"a": 1}'}
