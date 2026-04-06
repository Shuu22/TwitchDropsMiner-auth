import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import src.web.app as web_app


class TestWebUIAuth(unittest.TestCase):
    def setUp(self):
        self.original_failures = web_app._auth_failures.copy()

    def tearDown(self):
        web_app._auth_failures.clear()
        web_app._auth_failures.update(self.original_failures)

    def test_basic_auth_validation_success(self):
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            self.assertTrue(web_app._has_valid_basic_auth("Basic YWRtaW46c2VjcmV0"))

    def test_basic_auth_validation_failure(self):
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            self.assertFalse(web_app._has_valid_basic_auth("Basic YWRtaW46d3Jvbmc="))

    def test_auth_is_active_when_enabled(self):
        original_client = web_app.twitch_client
        try:
            web_app.twitch_client = SimpleNamespace(
                settings=SimpleNamespace(webui_auth_enabled=True)
            )
            web_app._refresh_auth_cache(force=True)
            self.assertTrue(web_app._is_webui_auth_active())
        finally:
            web_app.twitch_client = original_client

    def test_auth_is_configured_only_when_credentials_exist(self):
        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "admin",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "secret",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            self.assertTrue(web_app._is_webui_auth_configured())

        with patch.dict(
            os.environ,
            {
                web_app.WEBUI_AUTH_USERNAME_ENV: "",
                web_app.WEBUI_AUTH_PASSWORD_ENV: "",
            },
            clear=False,
        ):
            web_app._refresh_auth_cache(force=True)
            self.assertFalse(web_app._is_webui_auth_configured())

    def test_cors_origins_default_to_wildcard_when_env_missing(self):
        with patch.dict(os.environ, {web_app.WEBUI_ALLOWED_ORIGINS_ENV: ""}, clear=False):
            self.assertEqual(web_app._get_cors_allowed_origins(), ["*"])

    def test_cors_origins_reads_explicit_list_from_env(self):
        with patch.dict(
            os.environ,
            {web_app.WEBUI_ALLOWED_ORIGINS_ENV: "https://a.example, https://b.example"},
            clear=False,
        ):
            self.assertEqual(
                web_app._get_cors_allowed_origins(), ["https://a.example", "https://b.example"]
            )

    def test_cors_origins_ignores_invalid_entries(self):
        with patch.dict(
            os.environ,
            {web_app.WEBUI_ALLOWED_ORIGINS_ENV: "https://valid.example,invalid-origin,ftp://bad"},
            clear=False,
        ):
            self.assertEqual(web_app._get_cors_allowed_origins(), ["https://valid.example"])

    def test_cors_origins_with_only_invalid_entries_denies_cross_origin(self):
        with patch.dict(
            os.environ,
            {web_app.WEBUI_ALLOWED_ORIGINS_ENV: "bad,still-bad"},
            clear=False,
        ):
            self.assertEqual(web_app._get_cors_allowed_origins(), [])

    def test_auth_rate_limit_blocks_after_repeated_failures(self):
        client_key = "test-client"
        for _ in range(web_app._AUTH_MAX_FAILURES):
            web_app._register_auth_failure(client_key)

        self.assertFalse(web_app._check_auth_rate_limit(client_key))
        web_app._clear_auth_failures(client_key)
        self.assertTrue(web_app._check_auth_rate_limit(client_key))

    def test_auth_failure_pruning_removes_old_entries(self):
        web_app._auth_failures["stale-client"] = (1, 0.0, 0.0)
        web_app._prune_auth_failures(now=web_app._AUTH_WINDOW_SECONDS + 1)
        self.assertNotIn("stale-client", web_app._auth_failures)
