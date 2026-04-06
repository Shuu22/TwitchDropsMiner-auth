import asyncio
import base64
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import src.web.app as web_app


def _auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request_with_auth(auth_header: str | None) -> Request:
    headers = []
    if auth_header:
        headers.append((b"authorization", auth_header.encode("utf-8")))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/diagnostics",
        "raw_path": b"/api/diagnostics",
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive=receive)


class _GuiStub:
    def set_socketio(self, _sio):
        return None


class TestDiagnosticsMode(unittest.TestCase):
    def setUp(self):
        self.original_twitch_client = web_app.twitch_client
        self.original_gui_manager = web_app.gui_manager
        self.original_diagnostics = web_app._diagnostics_enabled
        self.original_failures = web_app._auth_failures.copy()

        self.twitch_client = SimpleNamespace(
            settings=SimpleNamespace(webui_auth_enabled=True, proxy="http://user:pass@proxy:8080")
        )
        web_app.twitch_client = self.twitch_client
        web_app.gui_manager = SimpleNamespace()
        web_app._diagnostics_enabled = False
        web_app._auth_failures.clear()

    def tearDown(self):
        web_app.twitch_client = self.original_twitch_client
        web_app.gui_manager = self.original_gui_manager
        web_app._diagnostics_enabled = self.original_diagnostics
        web_app._auth_failures.clear()
        web_app._auth_failures.update(self.original_failures)

    def test_hidden_by_default_and_enable_disable_cycle(self):
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            request = _request_with_auth(_auth_header("admin", "secret"))

            status_before = asyncio.run(web_app.diagnostics_status(request))
            self.assertFalse(status_before["enabled"])

            enabled = asyncio.run(web_app.enable_diagnostics(request))
            self.assertTrue(enabled["enabled"])

            status_after = asyncio.run(web_app.diagnostics_status(request))
            self.assertTrue(status_after["enabled"])

            disabled = asyncio.run(web_app.disable_diagnostics(request))
            self.assertFalse(disabled["enabled"])

    def test_auth_required_and_auth_enabled_required(self):
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)

            with self.assertRaises(HTTPException):
                asyncio.run(web_app.enable_diagnostics(_request_with_auth(None)))

            self.twitch_client.settings.webui_auth_enabled = False
            web_app._refresh_auth_cache(force=True)
            with self.assertRaises(HTTPException):
                asyncio.run(
                    web_app.enable_diagnostics(
                        _request_with_auth(_auth_header("admin", "secret"))
                    )
                )

    def test_direct_diagnostics_access_denied_when_disabled(self):
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            request = _request_with_auth(_auth_header("admin", "secret"))
            with self.assertRaises(HTTPException):
                asyncio.run(web_app.get_diagnostics(request))

    def test_restart_resets_runtime_state(self):
        web_app._diagnostics_enabled = True
        web_app.set_managers(_GuiStub(), self.twitch_client)
        self.assertFalse(web_app._diagnostics_enabled)

    def test_diagnostics_redacts_sensitive_values(self):
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            request = _request_with_auth(_auth_header("admin", "secret"))
            asyncio.run(web_app.enable_diagnostics(request))
            payload = asyncio.run(web_app.get_diagnostics(request))

        self.assertIn("proxy", payload)
        self.assertTrue(payload["proxy"]["configured"])
        self.assertTrue(payload["proxy"]["contains_credentials"])
        self.assertNotIn("proxy_url", payload)
        self.assertNotIn("authorization", str(payload).lower())
        self.assertNotIn("password", str(payload).lower())
