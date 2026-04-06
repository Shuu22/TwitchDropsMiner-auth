see AGENTS.md

Web UI optional authentication is controlled by `webui_auth_enabled` (settings) and
`TDM_WEBUI_AUTH_USERNAME`/`TDM_WEBUI_AUTH_PASSWORD` (environment variables).
Production CORS allowlist can be configured with `TDM_WEBUI_ALLOWED_ORIGINS`.
Only valid `http(s)://host[:port]` origins are accepted.
Auth env values are cached briefly to reduce per-request overhead.
Use reverse proxy TLS/rate limiting for internet-exposed deployments.
Auth behavior tests include `tests/test_webui_auth.py` and `tests/test_webui_auth_middleware.py`.
Diagnostics mode is runtime-only and must not persist across restarts.
Diagnostics UI should provide explicit status/error feedback for enable/disable and refresh status periodically.
Debounced settings writes should flush on unload with `sendBeacon` fallback when available.
README includes a general runtime operation section for Web UI auth/diagnostics usage.
When auth is enabled without credentials, the Web UI protection fails closed.
