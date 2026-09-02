from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.server import mount_frontend


def test_serves_index_at_root(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><title>app</title>", encoding="utf-8"
    )
    app = FastAPI()
    mount_frontend(app, dist)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "app" in r.text


def test_spa_fallback_for_unknown_route(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("INDEX", encoding="utf-8")
    app = FastAPI()
    mount_frontend(app, dist)
    client = TestClient(app)
    r = client.get("/login")
    assert r.status_code == 200
    assert r.text == "INDEX"


def test_api_path_without_router_returns_404(tmp_path: Path) -> None:
    """Несуществующий /api/* — 404, а не index.html.

    Catch-all SPA перехватывает любой GET, поэтому опечатка в пути или вызов
    вырезанного эндпоинта возвращались фронту как 200 + HTML: resp.ok истинно,
    а resp.json() падает на «Unexpected token <». Ошибка маскируется под
    ошибку парсинга вместо честного «эндпоинта нет».
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("INDEX", encoding="utf-8")
    app = FastAPI()
    mount_frontend(app, dist)
    client = TestClient(app)
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert "INDEX" not in r.text


def test_api_prefixed_page_route_still_falls_back(tmp_path: Path) -> None:
    """Гейт срабатывает ровно на /api и /api/..., а не на любом пути с «api»
    в начале сегмента: страница /apidocs — обычный клиентский роут."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("INDEX", encoding="utf-8")
    app = FastAPI()
    mount_frontend(app, dist)
    client = TestClient(app)
    assert client.get("/apidocs").text == "INDEX"


def test_noop_when_dist_missing(tmp_path: Path) -> None:
    app = FastAPI()
    mount_frontend(app, tmp_path / "nope")  # не падает
    client = TestClient(app)
    assert client.get("/").status_code == 404


def test_spa_blocks_encoded_path_traversal(tmp_path: Path) -> None:
    """URL-кодированные `..` (%2e%2e%2f) не должны утекать файл вне dist."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("INDEX", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    app = FastAPI()
    mount_frontend(app, dist)
    client = TestClient(app)
    for attack in ("/..%2fsecret.txt", "/%2e%2e%2fsecret.txt", "/..%2f..%2fsecret.txt"):
        r = client.get(attack)
        assert "TOPSECRET" not in r.text, f"traversal leaked via {attack}"
