"""End-to-end (Playwright) tests for the per-user API-key panel in the web UI.

These drive the *real* built SPA (``web/dist``) in a headless Chromium and
exercise ``ApiKeyPanel`` through the same path a user takes:

    load app → Sidebar profile button → Settings modal → «API-ключ» tab.

The FastAPI backend is *not* started. Instead every ``/api/**`` call is
intercepted at the browser layer (Playwright request routing) and answered by
:class:`FakeBackend`, so the tests are hermetic and order-independent — each
test spins up its own browser context with its own backend state.

Requirements (all auto-skipped when missing, so the module never hard-fails):
  * ``playwright`` importable                → module-level ``importorskip``
  * ``web/dist/index.html`` present (built)  → module-level ``skipif``
  * a Chromium browser binary installed      → per-test ``pytest.skip``
    (run ``python -m playwright install chromium`` to provision it)

Run:  pytest tests/integration/web/test_apikey_ui.py -v
"""
from __future__ import annotations

import json
import re
import threading
from contextlib import asynccontextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

# Hard requirement: without Playwright there is nothing to run.
pytest.importorskip("playwright.async_api")
# Imported after importorskip so a missing dependency skips rather than errors.
from playwright.async_api import Error as PlaywrightError  # noqa: E402
from playwright.async_api import async_playwright, expect  # noqa: E402

# repo_root/web/dist  (this file is tests/integration/web/test_apikey_ui.py)
DIST = Path(__file__).resolve().parents[3] / "web" / "dist"

pytestmark = pytest.mark.skipif(
    not (DIST / "index.html").exists(),
    reason="web/dist not built — run `npm run build` in web/",
)

# A well-formed key: "sk-ant-" + filler + a recognisable last-4 ("WXYZ").
NEW_KEY = "sk-ant-api03-" + "a" * 40 + "WXYZ"


# --------------------------------------------------------------------------- #
# Static file server for the built SPA
# --------------------------------------------------------------------------- #
class _SpaHandler(SimpleHTTPRequestHandler):
    """Serve ``web/dist`` with a single-page-app fallback to index.html."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST), **kwargs)

    def log_message(self, *args):  # silence the per-request stderr spam
        pass

    def do_GET(self):  # noqa: N802 (stdlib naming)
        path = urlparse(self.path).path
        candidate = DIST / path.lstrip("/")
        # Client-side routes ("/", "/settings", …) have no file on disk and no
        # extension — serve the SPA shell so the router can take over.
        if path != "/" and not candidate.is_file() and "." not in Path(path).name:
            self.path = "/index.html"
        return super().do_GET()


@pytest.fixture(scope="session")
def app_url():
    """Serve the built SPA on an ephemeral port for the whole test session."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SpaHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# Fake backend — answers the handful of /api/** calls the app makes
# --------------------------------------------------------------------------- #
class FakeBackend:
    """In-memory stand-in for the REST API, driven via Playwright routing.

    Only ``/api/me`` and ``/api/apikey`` carry meaningful behaviour; the few
    other bootstrap calls (projects/sessions/settings/model/…) get benign
    empty answers so the app renders far enough to reach the settings modal.
    """

    def __init__(
        self,
        *,
        is_admin: bool = False,
        key: str | None = None,
        get_status: int = 200,
        put_mode: str = "ok",  # "ok" | "invalid"
    ):
        self.user = {"id": 42, "username": "TestUser", "is_admin": is_admin}
        self.get_status = get_status
        self.put_mode = put_mode
        self.put_calls = 0
        self.delete_calls = 0
        if key:
            self.meta = {
                "status": "active",
                "last4": key[-4:],
                "created_at": "2026-07-03T00:00:00Z",
                "updated_at": "2026-07-03T00:00:00Z",
            }
        else:
            self.meta = {
                "status": None,
                "last4": None,
                "created_at": None,
                "updated_at": None,
            }

    async def handle_route(self, route):
        req = route.request
        path = urlparse(req.url).path
        method = req.method
        if path.endswith("/api/me"):
            await self._json(route, 200, self.user)
        elif path.endswith("/api/apikey"):
            await self._apikey(route, method)
        elif path.endswith("/api/projects") or path.endswith("/api/sessions"):
            await self._raw(route, 200, "[]")
        else:
            # Everything else (settings, model, connections, …) — harmless empty.
            await self._raw(route, 200, "{}")

    async def _apikey(self, route, method: str):
        if method == "GET":
            if self.get_status != 200:
                await self._json(
                    route, self.get_status, {"detail": "api key store not configured"}
                )
                return
            await self._json(route, 200, {**self.meta, "privileged": self.user["is_admin"]})
        elif method == "PUT":
            self.put_calls += 1
            if self.put_mode == "invalid":
                await self._json(
                    route,
                    422,
                    {"detail": "API key failed validation (401 Unauthorized)"},
                )
                return
            body = json.loads(route.request.post_data or "{}")
            key = body.get("api_key", "")
            self.meta = {
                "status": "active",
                "last4": key[-4:],
                "created_at": "2026-07-03T00:00:00Z",
                "updated_at": "2026-07-03T00:00:00Z",
            }
            await self._json(route, 200, {"status": "active", "message": "API key saved"})
        elif method == "DELETE":
            self.delete_calls += 1
            self.meta = {"status": None, "last4": None, "created_at": None, "updated_at": None}
            await self._json(route, 200, {"message": "API key deleted"})
        else:
            await self._raw(route, 405, "{}")

    @staticmethod
    async def _json(route, status: int, payload):
        await route.fulfill(
            status=status, content_type="application/json", body=json.dumps(payload)
        )

    @staticmethod
    async def _raw(route, status: int, body: str):
        await route.fulfill(status=status, content_type="application/json", body=body)


# --------------------------------------------------------------------------- #
# Navigation helper: open the app → Settings modal → «API-ключ» tab
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def apikey_panel(app_url: str, backend: FakeBackend, *, open_tab: bool = True):
    """Yield a Page parked on the API-key panel, tearing the browser down after."""
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except PlaywrightError as exc:  # browser binary not installed
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            context = await browser.new_context()
            await context.route("**/api/**", backend.handle_route)
            page = await context.new_page()
            page.set_default_timeout(20_000)
            await page.goto(f"{app_url}/")
            # Sidebar footer profile button opens the settings modal.
            await page.get_by_role("button", name=re.compile("TestUser")).click()
            if open_tab:
                await page.get_by_role("button", name="API-ключ").click()
            yield page
        finally:
            await browser.close()


# --------------------------------------------------------------------------- #
# 1. Load settings and navigate to the API-key tab
# --------------------------------------------------------------------------- #
async def test_navigate_to_apikey_tab_shows_panel(app_url):
    backend = FakeBackend(is_admin=False)
    async with apikey_panel(app_url, backend) as page:
        # The panel's hallmark controls are present.
        await expect(page.get_by_text("Текущий ключ")).to_be_visible()
        await expect(page.get_by_placeholder("sk-ant-…")).to_be_visible()
        await expect(page.get_by_role("button", name="Сохранить")).to_be_visible()


# --------------------------------------------------------------------------- #
# 2. Display current key status (masked key + status badge)
# --------------------------------------------------------------------------- #
async def test_displays_current_key_status(app_url):
    backend = FakeBackend(key="sk-ant-" + "0" * 40 + "AB12")
    async with apikey_panel(app_url, backend) as page:
        await expect(page.get_by_text("sk-…AB12")).to_be_visible()
        await expect(page.get_by_text("активен")).to_be_visible()


# --------------------------------------------------------------------------- #
# 3. Save a new valid key → success + persisted (masked) key
# --------------------------------------------------------------------------- #
async def test_save_valid_key_shows_success(app_url):
    backend = FakeBackend()  # starts with no key stored
    async with apikey_panel(app_url, backend) as page:
        await expect(page.get_by_text("не задан")).to_be_visible()
        await page.get_by_placeholder("sk-ant-…").fill(NEW_KEY)
        await page.get_by_role("button", name="Сохранить").click()

        await expect(page.get_by_text("API key saved")).to_be_visible()
        # Panel reloaded and now shows the masked, freshly-saved key.
        await expect(page.get_by_text("sk-…WXYZ")).to_be_visible()
        assert backend.put_calls == 1


# --------------------------------------------------------------------------- #
# 4. Invalid format is rejected client-side, before any API call
# --------------------------------------------------------------------------- #
async def test_invalid_format_rejected_client_side(app_url):
    backend = FakeBackend()
    async with apikey_panel(app_url, backend) as page:
        await page.get_by_placeholder("sk-ant-…").fill("not-a-real-key")
        await page.get_by_role("button", name="Сохранить").click()

        await expect(page.get_by_text(re.compile("должен начинаться с sk-ant-"))).to_be_visible()
        # The client short-circuits — the backend PUT is never reached.
        assert backend.put_calls == 0


# --------------------------------------------------------------------------- #
# 5. Server probe failure (422) surfaces the error detail
# --------------------------------------------------------------------------- #
async def test_probe_failure_shows_server_error(app_url):
    backend = FakeBackend(put_mode="invalid")
    async with apikey_panel(app_url, backend) as page:
        await page.get_by_placeholder("sk-ant-…").fill(NEW_KEY)
        await page.get_by_role("button", name="Сохранить").click()

        await expect(
            page.get_by_text(re.compile("failed validation \\(401 Unauthorized\\)"))
        ).to_be_visible()
        assert backend.put_calls == 1


# --------------------------------------------------------------------------- #
# 6. Delete an existing key → success + panel refreshes to "не задан"
#    (ApiKeyPanel deletes immediately; there is no separate confirm dialog.)
# --------------------------------------------------------------------------- #
async def test_delete_key_refreshes_panel(app_url):
    backend = FakeBackend(key="sk-ant-" + "0" * 40 + "AB12")
    async with apikey_panel(app_url, backend) as page:
        await expect(page.get_by_text("sk-…AB12")).to_be_visible()
        await page.get_by_role("button", name="Удалить").click()

        await expect(page.get_by_text("API key deleted")).to_be_visible()
        await expect(page.get_by_text("не задан")).to_be_visible()
        assert backend.delete_calls == 1


# --------------------------------------------------------------------------- #
# 7. Privilege-dependent explainer text
# --------------------------------------------------------------------------- #
async def test_admin_sees_optional_key_text(app_url):
    backend = FakeBackend(is_admin=True)
    async with apikey_panel(app_url, backend) as page:
        await expect(
            page.get_by_text(re.compile("по умолчанию подписка сервиса"))
        ).to_be_visible()


async def test_non_admin_sees_required_key_text(app_url):
    backend = FakeBackend(is_admin=False)
    async with apikey_panel(app_url, backend) as page:
        await expect(
            page.get_by_text(re.compile("Без ключа отправка сообщений недоступна"))
        ).to_be_visible()
