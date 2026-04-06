import asyncio
import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import JSONResponse

import src.web.app as web_app


def _basic_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class TestWebUIAuthMiddleware(unittest.TestCase):
    def setUp(self):
        self.original_twitch_client = web_app.twitch_client
        self.original_gui_manager = web_app.gui_manager
        self.original_warning_flag = web_app._auth_env_warning_logged
        self.original_failures = web_app._auth_failures.copy()

        self.gui_manager = SimpleNamespace(
            status=SimpleNamespace(get=lambda: "Idle"),
            login=SimpleNamespace(get_status=lambda: {"status": "Logged out"}),
        )
        self.twitch_client = SimpleNamespace(
            settings=SimpleNamespace(webui_auth_enabled=False),
            get_manual_mode_info=lambda: {"enabled": False},
        )
        web_app.gui_manager = self.gui_manager
        web_app.twitch_client = self.twitch_client
        web_app._auth_env_warning_logged = False
        web_app._auth_failures.clear()
        web_app._refresh_auth_cache(force=True)

    def tearDown(self):
        web_app.twitch_client = self.original_twitch_client
        web_app.gui_manager = self.original_gui_manager
        web_app._auth_env_warning_logged = self.original_warning_flag
        web_app._auth_failures.clear()
        web_app._auth_failures.update(self.original_failures)

    async def _call_middleware(self, path: str, headers: dict[str, str] | None = None):
        raw_headers = []
        if headers:
            raw_headers = [(k.lower().encode("utf-8"), v.encode("utf-8")) for k, v in headers.items()]

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": raw_headers,
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, receive=receive)

        async def call_next(_request):
            return JSONResponse({"ok": True}, status_code=200)

        return await web_app.webui_auth_middleware(request, call_next)

    def test_auth_disabled_allows_api_access_without_header(self):
        self.twitch_client.settings.webui_auth_enabled = False
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "any-user",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "any-pass",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            response = asyncio.run(self._call_middleware("/api/status"))

        self.assertEqual(response.status_code, 200)

    def test_auth_enabled_with_valid_credentials_allows_access(self):
        self.twitch_client.settings.webui_auth_enabled = True
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            response = asyncio.run(
                self._call_middleware("/api/status", headers=_basic_header("admin", "secret"))
            )

        self.assertEqual(response.status_code, 200)

    def test_auth_enabled_with_invalid_credentials_denies_access(self):
        self.twitch_client.settings.webui_auth_enabled = True
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            response = asyncio.run(
                self._call_middleware("/api/status", headers=_basic_header("admin", "wrong"))
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(json.loads(response.body), {"detail": "Unauthorized"})

    def test_auth_enabled_without_header_denies_access(self):
        self.twitch_client.settings.webui_auth_enabled = True
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            response = asyncio.run(self._call_middleware("/api/status"))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(json.loads(response.body), {"detail": "Unauthorized"})

    def test_auth_enabled_without_environment_credentials_fails_closed(self):
        self.twitch_client.settings.webui_auth_enabled = True
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            response = asyncio.run(self._call_middleware("/api/status"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body), {"detail": "Service unavailable"})

    def test_socket_connect_rejects_invalid_auth(self):
        self.twitch_client.settings.webui_auth_enabled = True
        web_app.gui_manager = None
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            with self.assertRaises(ConnectionRefusedError):
                asyncio.run(
                    web_app.connect(
                        "sid-1",
                        {"REMOTE_ADDR": "127.0.0.1", "HTTP_AUTHORIZATION": "Basic YmFkOmNyZWRz"},
                    )
                )

    def test_socket_connect_allows_valid_auth(self):
        self.twitch_client.settings.webui_auth_enabled = True
        web_app.gui_manager = None
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            result = asyncio.run(
                web_app.connect(
                    "sid-2",
                    {"REMOTE_ADDR": "127.0.0.2", "HTTP_AUTHORIZATION": _basic_header("admin", "secret")["Authorization"]},
                )
            )

        self.assertIsNone(result)
