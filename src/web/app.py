from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import socketio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


if TYPE_CHECKING:
    import uvicorn

    from src.core.client import Twitch
    from src.web.gui_manager import WebGUIManager


logger = logging.getLogger("TwitchDrops")

WEBUI_AUTH_USERNAME_ENV = "TDM_WEBUI_AUTH_USERNAME"
WEBUI_AUTH_PASSWORD_ENV = "TDM_WEBUI_AUTH_PASSWORD"
WEBUI_ALLOWED_ORIGINS_ENV = "TDM_WEBUI_ALLOWED_ORIGINS"


def _get_cors_allowed_origins() -> list[str]:
    raw_origins = os.getenv(WEBUI_ALLOWED_ORIGINS_ENV, "").strip()
    if not raw_origins:
        return ["*"]

    valid_origins: list[str] = []
    for origin in [origin.strip() for origin in raw_origins.split(",") if origin.strip()]:
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            logger.warning(
                f"Ignoring invalid CORS origin from {WEBUI_ALLOWED_ORIGINS_ENV}: {origin}"
            )
            continue
        normalized_origin = f"{parsed.scheme}://{parsed.netloc}"
        valid_origins.append(normalized_origin)

    if valid_origins:
        return valid_origins

    logger.warning(
        f"No valid origins found in {WEBUI_ALLOWED_ORIGINS_ENV}. Cross-origin requests will be denied."
    )
    return []


CORS_ALLOWED_ORIGINS = _get_cors_allowed_origins()

# Create FastAPI app
app = FastAPI(title="Twitch Drops Miner Web", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create Socket.IO server
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=CORS_ALLOWED_ORIGINS,
    logger=False,
    engineio_logger=False,
)

# Wrap with ASGI app
socket_app = socketio.ASGIApp(sio, app)

# Global references (set by main.py)
gui_manager: WebGUIManager | None = None
twitch_client: Twitch | None = None
_server_instance: uvicorn.Server | None = None
_auth_env_warning_logged = False
_auth_cache_last_refresh = 0.0
_cached_auth_credentials: tuple[str, str] = ("", "")
_cached_auth_enabled = False
_AUTH_CACHE_TTL_SECONDS = 5.0
_AUTH_WINDOW_SECONDS = 60.0
_AUTH_MAX_FAILURES = 20
_AUTH_BLOCK_SECONDS = 60.0
_auth_failures: dict[str, tuple[int, float, float]] = {}
_diagnostics_enabled = False
_app_start_time = time.monotonic()


def _refresh_auth_cache(*, force: bool = False):
    global _auth_cache_last_refresh, _cached_auth_credentials, _cached_auth_enabled

    now = time.monotonic()
    if not force and (now - _auth_cache_last_refresh) < _AUTH_CACHE_TTL_SECONDS:
        return

    _cached_auth_credentials = (
        os.getenv(WEBUI_AUTH_USERNAME_ENV, ""),
        os.getenv(WEBUI_AUTH_PASSWORD_ENV, ""),
    )
    _cached_auth_enabled = bool(
        twitch_client and getattr(twitch_client.settings, "webui_auth_enabled", False)
    )
    _auth_cache_last_refresh = now


def _get_client_key(request: Request) -> str:
    return request.client.host if request.client and request.client.host else "unknown"


def _check_auth_rate_limit(client_key: str) -> bool:
    now = time.monotonic()
    _prune_auth_failures(now)
    entry = _auth_failures.get(client_key)
    if not entry:
        return True

    count, window_start, blocked_until = entry
    if blocked_until > now:
        return False

    if (now - window_start) > _AUTH_WINDOW_SECONDS:
        _auth_failures.pop(client_key, None)
        return True

    return True


def _prune_auth_failures(now: float | None = None):
    now = time.monotonic() if now is None else now
    for key, (_, window_start, blocked_until) in list(_auth_failures.items()):
        if blocked_until > now:
            continue
        if (now - window_start) > _AUTH_WINDOW_SECONDS:
            _auth_failures.pop(key, None)


def _register_auth_failure(client_key: str):
    now = time.monotonic()
    _prune_auth_failures(now)
    count, window_start, blocked_until = _auth_failures.get(client_key, (0, now, 0.0))

    if (now - window_start) > _AUTH_WINDOW_SECONDS:
        count = 0
        window_start = now
        blocked_until = 0.0

    count += 1
    if count >= _AUTH_MAX_FAILURES:
        blocked_until = now + _AUTH_BLOCK_SECONDS

    _auth_failures[client_key] = (count, window_start, blocked_until)


def _clear_auth_failures(client_key: str):
    _auth_failures.pop(client_key, None)


def _is_request_authenticated(request: Request) -> bool:
    return _has_valid_basic_auth(request.headers.get("Authorization"))


def _build_diagnostics_payload() -> dict:
    uptime_seconds = int(time.monotonic() - _app_start_time)
    auth_enabled = _is_webui_auth_active()
    auth_configured = _is_webui_auth_configured() if auth_enabled else False

    proxy_raw = ""
    if twitch_client and hasattr(twitch_client, "settings"):
        proxy_raw = getattr(twitch_client.settings, "proxy", "") or ""

    return {
        "uptime_seconds": uptime_seconds,
        "webui_auth": {
            "enabled": auth_enabled,
            "configured": auth_configured,
        },
        "cors": {
            "mode": "wildcard" if CORS_ALLOWED_ORIGINS == ["*"] else "allowlist",
            "origins_count": len(CORS_ALLOWED_ORIGINS),
        },
        "rate_limit": {
            "window_seconds": int(_AUTH_WINDOW_SECONDS),
            "max_failures": _AUTH_MAX_FAILURES,
            "block_seconds": int(_AUTH_BLOCK_SECONDS),
            "tracked_clients": len(_auth_failures),
        },
        "proxy": {
            "configured": bool(proxy_raw),
            "contains_credentials": "@" in proxy_raw if proxy_raw else False,
        },
    }


def _get_webui_auth_credentials() -> tuple[str, str]:
    _refresh_auth_cache()
    return _cached_auth_credentials


def _is_webui_auth_active() -> bool:
    _refresh_auth_cache()
    return _cached_auth_enabled


def _is_webui_auth_configured() -> bool:
    global _auth_env_warning_logged

    username, password = _get_webui_auth_credentials()
    if username and password:
        return True

    if not _auth_env_warning_logged:
        logger.warning(
            "Web UI auth was enabled in settings, but credentials are missing in environment."
        )
        _auth_env_warning_logged = True
    return False


def _has_valid_basic_auth(auth_header: str | None) -> bool:
    if not auth_header or not auth_header.startswith("Basic "):
        return False

    try:
        encoded_part = auth_header.split(" ", 1)[1]
        decoded = base64.b64decode(encoded_part).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return False

    expected_username, expected_password = _get_webui_auth_credentials()
    if not expected_username or not expected_password:
        return False

    return hmac.compare_digest(username, expected_username) and hmac.compare_digest(
        password, expected_password
    )


def _is_protected_webui_path(path: str) -> bool:
    if path == "/":
        return True
    return path.startswith("/api/") or path.startswith("/static/") or path.startswith("/socket.io")


@app.middleware("http")
async def webui_auth_middleware(request: Request, call_next):
    if _is_protected_webui_path(request.url.path):
        if _is_webui_auth_active():
            client_key = _get_client_key(request)
            if not _check_auth_rate_limit(client_key):
                return JSONResponse(status_code=429, content={"detail": "Too many requests"})
            if not _is_webui_auth_configured():
                return JSONResponse(status_code=503, content={"detail": "Service unavailable"})
            if not _has_valid_basic_auth(request.headers.get("Authorization")):
                _register_auth_failure(client_key)
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized"},
                    headers={"WWW-Authenticate": "Basic"},
                )
            _clear_auth_failures(client_key)
    return await call_next(request)


def set_managers(gui: WebGUIManager, twitch: Twitch):
    """Called by main.py to set up references"""
    global gui_manager, twitch_client, _diagnostics_enabled
    gui_manager = gui
    twitch_client = twitch
    _diagnostics_enabled = False
    _refresh_auth_cache(force=True)
    gui.set_socketio(sio)


# Pydantic models for API
class LoginRequest(BaseModel):
    username: str
    password: str
    token: str = ""


class ChannelSelectRequest(BaseModel):
    channel_id: int


class SettingsUpdate(BaseModel):
    games_to_watch: list[str] | None = None
    dark_mode: bool | None = None
    language: str | None = None
    proxy: str | None = None
    connection_quality: int | None = None
    minimum_refresh_interval_minutes: int | None = None
    inventory_filters: dict | None = None
    mining_benefits: dict[str, bool] | None = None
    webui_auth_enabled: bool | None = None


class ProxyVerifyRequest(BaseModel):
    proxy: str


# ==================== REST API Endpoints ====================


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the main web interface"""
    # Web files are in project_root/web/, we're in project_root/src/web/
    web_dir = Path(__file__).parent.parent.parent / "web"
    index_file = web_dir / "index.html"
    logger.debug(
        f"Looking for web files: __file__={__file__}, web_dir={web_dir}, index_file={index_file}, exists={index_file.exists()}"
    )
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse(
        content=f"<h1>Twitch Drops Miner</h1><p>Web interface files not found. Please check installation.</p><p>Debug: Looking for {index_file}</p>",
        status_code=500,
    )


@app.get("/healthz")
async def healthcheck():
    """Lightweight health endpoint for container checks."""
    return {"ok": True}


@app.get("/api/status")
async def get_status():
    """Get current application status"""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {
        "status": gui_manager.status.get(),
        "login": gui_manager.login.get_status(),
        "manual_mode": twitch_client.get_manual_mode_info(),
    }


@app.get("/api/diagnostics/status")
async def diagnostics_status(request: Request):
    if not _is_webui_auth_active():
        raise HTTPException(status_code=403, detail="Forbidden")
    if not _is_request_authenticated(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"enabled": _diagnostics_enabled}


@app.post("/api/diagnostics/enable")
async def enable_diagnostics(request: Request):
    global _diagnostics_enabled
    if not _is_webui_auth_active():
        raise HTTPException(status_code=403, detail="Forbidden")
    if not _is_request_authenticated(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    _diagnostics_enabled = True
    return {"enabled": True}


@app.post("/api/diagnostics/disable")
async def disable_diagnostics(request: Request):
    global _diagnostics_enabled
    if not _is_webui_auth_active():
        raise HTTPException(status_code=403, detail="Forbidden")
    if not _is_request_authenticated(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    _diagnostics_enabled = False
    return {"enabled": False}


@app.get("/api/diagnostics")
async def get_diagnostics(request: Request):
    if not _is_webui_auth_active():
        raise HTTPException(status_code=403, detail="Forbidden")
    if not _is_request_authenticated(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not _diagnostics_enabled:
        raise HTTPException(status_code=403, detail="Forbidden")
    return _build_diagnostics_payload()


@app.get("/api/channels")
async def get_channels():
    """Get list of tracked channels"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"channels": gui_manager.channels.get_channels()}


@app.post("/api/channels/select")
async def select_channel(request: ChannelSelectRequest):
    """Select a channel to watch"""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    # Validate channel exists
    channel = twitch_client.channels.get(request.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Validate channel has a game
    if not channel.game:
        raise HTTPException(status_code=400, detail="Channel is not playing any game")

    # Warn if channel has no drops (shouldn't happen if GUI is filtering correctly)
    if not any(campaign.can_earn(channel) for campaign in twitch_client.inventory):
        logger.warning(f"User selected channel {channel.name} but it has no available drops")

    gui_manager.select_channel(request.channel_id)

    # Trigger channel switch to apply the selection
    from src.config import State

    twitch_client.change_state(State.CHANNEL_SWITCH)

    return {"success": True}


@app.get("/api/campaigns")
async def get_campaigns():
    """Get campaign inventory"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"campaigns": gui_manager.inv.get_campaigns()}


@app.get("/api/console")
async def get_console_history():
    """Get console output history"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"lines": gui_manager.output.get_history()}


@app.get("/api/settings")
async def get_settings():
    """Get current settings"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return gui_manager.settings.get_settings()


@app.get("/api/languages")
async def get_languages():
    """Get available languages"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return gui_manager.settings.get_languages()


@app.get("/api/translations")
async def get_translations():
    """Get translations for current language"""
    from src.i18n.translator import _

    # Return the full Translation object
    return _.t


@app.post("/api/settings")
async def update_settings(settings: SettingsUpdate):
    """Update application settings"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    settings_dict = settings.dict(exclude_unset=True)
    gui_manager.settings.update_settings(settings_dict)
    if "webui_auth_enabled" in settings_dict:
        _refresh_auth_cache(force=True)
    return {"success": True, "settings": gui_manager.settings.get_settings()}


@app.post("/api/settings/verify-proxy")
async def verify_proxy(request: ProxyVerifyRequest):
    """Verify proxy connectivity"""
    import time

    import aiohttp

    proxy_url = request.proxy.strip()
    if not proxy_url:
        return {"success": False, "message": "Proxy URL is empty"}

    try:
        start_time = time.time()
        # Test connection to Twitch
        async with (
            aiohttp.ClientSession() as session,
            session.get("https://www.twitch.tv", proxy=proxy_url, timeout=10) as response,
        ):
            # Just checking if we can connect and get a response
            if response.status < 500:
                latency = round((time.time() - start_time) * 1000)
                return {
                    "success": True,
                    "message": f"Connected! ({latency}ms)",
                    "latency": latency,
                }
            else:
                return {
                    "success": False,
                    "message": f"Proxy reachable but returned {response.status}",
                }
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


@app.get("/api/version")
async def get_version():
    """Get current application version and check for updates"""
    import aiohttp

    from src.version import __version__

    current_version = __version__
    latest_version = None
    update_available = False
    download_url = None

    try:
        # Check GitHub API for latest release
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                "https://api.github.com/repos/rangermix/TwitchDropsMiner/releases/latest", timeout=5
            ) as response,
        ):
            if response.status == 200:
                data = await response.json()
                latest_version = data.get("tag_name", "").lstrip("v")
                download_url = data.get("html_url")

                # Compare versions (simple string comparison works for semantic versioning)
                if latest_version and latest_version > current_version:
                    update_available = True
    except Exception as e:
        logger.warning(f"Failed to check for updates: {str(e)}")

    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "download_url": download_url or "https://github.com/rangermix/TwitchDropsMiner/releases",
    }


@app.post("/api/login")
async def submit_login(login_data: LoginRequest):
    """Submit login credentials"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    gui_manager.login.submit_login(login_data.username, login_data.password, login_data.token)
    return {"success": True}


@app.post("/api/oauth/confirm")
async def confirm_oauth():
    """Confirm OAuth code has been entered by user"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    # Just set the event to signal the user has acknowledged the code
    gui_manager.login._login_event.set()
    return {"success": True}


@app.post("/api/reload")
async def trigger_reload():
    """Trigger application reload"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    from src.config import State

    twitch_client.change_state(State.INVENTORY_FETCH)
    return {"success": True}


@app.post("/api/close")
async def trigger_close():
    """Trigger application shutdown"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    twitch_client.close()
    return {"success": True}


@app.post("/api/mode/exit-manual")
async def exit_manual_mode():
    """Exit manual mode and return to automatic channel selection"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    if not twitch_client.is_manual_mode():
        return {"success": False, "message": "Not in manual mode"}

    twitch_client.exit_manual_mode("User requested")
    return {"success": True}


# ==================== Socket.IO Events ====================


@sio.event
async def connect(sid, environ, auth=None):
    """Client connected"""
    if _is_webui_auth_active():
        client_key = environ.get("REMOTE_ADDR", sid)
        if not _check_auth_rate_limit(client_key):
            raise ConnectionRefusedError("Too many requests")
        if not _is_webui_auth_configured():
            raise ConnectionRefusedError("Service unavailable")
        auth_header = environ.get("HTTP_AUTHORIZATION")
        if not _has_valid_basic_auth(auth_header):
            _register_auth_failure(client_key)
            raise ConnectionRefusedError("Unauthorized")
        _clear_auth_failures(client_key)

    logger.info(f"Web client connected: {sid}")

    # Send initial state to new client
    if gui_manager and twitch_client:
        await sio.emit(
            "initial_state",
            {
                "status": gui_manager.status.get(),
                "channels": gui_manager.channels.get_channels(),
                "campaigns": gui_manager.inv.get_campaigns(),
                "console": gui_manager.output.get_history(),
                "settings": gui_manager.settings.get_settings(),
                "login": gui_manager.login.get_status(),
                "manual_mode": twitch_client.get_manual_mode_info(),
                "current_drop": gui_manager.progress.get_current_drop(),
                "wanted_items": gui_manager.get_wanted_game_tree(),
            },
            room=sid,
        )


@sio.event
async def disconnect(sid):
    """Client disconnected"""
    logger.info(f"Web client disconnected: {sid}")


@sio.event
async def request_login(sid):
    """Client requested login form submission"""
    logger.info(f"Login request from client: {sid}")
    # The actual login data comes via REST API


@sio.event
async def request_reload(sid):
    """Client requested application reload"""
    if twitch_client:
        from src.config import State

        twitch_client.change_state(State.INVENTORY_FETCH)


@sio.event
async def get_wanted_items(sid):
    """Client requested wanted items list"""
    if gui_manager:
        await sio.emit("wanted_items_update", gui_manager.get_wanted_game_tree(), to=sid)


# Mount static files (CSS, JS, images)
# Web files are in project_root/web/, we're in project_root/src/web/
web_dir = Path(__file__).parent.parent.parent / "web"
if web_dir.exists():
    static_dir = web_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Development server runner
async def run_server(host: str = "0.0.0.0", port: int = 8080):
    """Run the web server (used for development/testing)"""
    global _server_instance
    import uvicorn

    config = uvicorn.Config(socket_app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    _server_instance = server
    try:
        await server.serve()
    finally:
        _server_instance = None


async def shutdown_server():
    """Gracefully shutdown the web server"""
    if _server_instance:
        logger.info("Setting server.should_exit = True")
        _server_instance.should_exit = True
        # Give the server a moment to process the shutdown signal
        # The uvicorn server checks should_exit periodically
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    # For standalone testing
    asyncio.run(run_server())
