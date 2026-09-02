from src.connections import catalog


def test_catalog_has_notion_and_github():
    ids = {s.id for s in catalog.CATALOG}
    assert {"notion", "github"} <= ids


def test_every_service_is_well_formed():
    for svc in catalog.CATALOG:
        assert svc.id and svc.name and svc.how_to_url
        assert svc.how_to_steps, f"{svc.id} needs how-to steps"
        cfg = svc.build_mcp_server("SECRET123")
        assert "SECRET123" in repr(cfg)
        assert "command" in cfg or "url" in cfg


def test_get_service_lookup():
    assert catalog.get_service("notion").name.lower().startswith("notion")
    assert catalog.get_service("nope") is None


def test_github_uses_remote_http_with_bearer():
    cfg = catalog.get_service("github").build_mcp_server("PAT123")
    assert cfg["type"] == "http"
    assert cfg["url"] == "https://api.githubcopilot.com/mcp/"
    assert cfg["headers"]["Authorization"] == "Bearer PAT123"


def test_notion_uses_stdio_with_token():
    cfg = catalog.get_service("notion").build_mcp_server("ntn_123")
    assert cfg["command"] == "npx"
    assert cfg["env"]["NOTION_TOKEN"] == "ntn_123"
