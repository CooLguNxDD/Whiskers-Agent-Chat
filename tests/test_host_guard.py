"""Host validation must also protect browser requests without Origin."""

import pytest


@pytest.mark.parametrize("path", ["/api/v1/channels", "/api/v1/events", "/api/v1/wait", "/mcp/"])
async def test_untrusted_host_without_origin_is_rejected(running, path):
    client, _ = running
    result = await client.get(path, headers={"Host": "attacker.example:8787"})
    assert result.status_code == 421


async def test_untrusted_write_without_origin_is_rejected(running):
    client, _ = running
    result = await client.post(
        "/api/v1/messages", headers={"Host": "attacker.example:8787"},
        json={"channel": "fleet", "author": "ada", "text": "must not be stored"},
    )
    assert result.status_code == 421
    page = await client.get("/api/v1/messages?channel=fleet")
    assert page.json()["messages"] == []


async def test_authenticated_docker_host_alias_is_supported(authed):
    client, _ = authed
    result = await client.get("/api/v1/channels", headers={
        "Host": "host.docker.internal:8787", "Authorization": "Bearer test-token",
    })
    assert result.status_code == 200
