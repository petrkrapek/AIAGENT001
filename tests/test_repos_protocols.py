"""The in-memory doubles must keep the interface of the Airtable repositories the tools rely on."""

import inspect

import pytest

from aiagent import repos
from memory_repos import memory_repos

PROTOCOLS = {"leads": repos.LeadRepo, "tasks": repos.TaskRepo, "audit": repos.AuditLog,
             "services": repos.ServiceRepo, "profile": repos.ProfileRepo, "blocklist": repos.Blocklist}


@pytest.mark.parametrize("name", PROTOCOLS)
def test_both_implementations_follow_the_protocol(name):
    airtable = repos.airtable_repos(client=None, tz="Europe/Prague")  # type: ignore[arg-type]
    memory = memory_repos()
    protocol = PROTOCOLS[name]
    for impl in (getattr(airtable, name), getattr(memory, name)):
        assert isinstance(impl, protocol), type(impl).__name__
        for method in (m for m, _ in inspect.getmembers(protocol, inspect.isfunction) if not m.startswith("_")):
            expected = list(inspect.signature(getattr(protocol, method)).parameters)[1:]
            actual = list(inspect.signature(getattr(impl, method)).parameters)
            assert actual == expected, f"{type(impl).__name__}.{method}{actual} != {expected}"
