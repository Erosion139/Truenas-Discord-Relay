"""aiohttp web server: JSON API + static single-page UI."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Any

import discord
from aiohttp import web

from .relay import ActivityLog, BotManager
from .rules import RuleError, normalize_rule
from .storage import Store

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "relay_session"
SESSION_TTL = 7 * 24 * 3600
PUBLIC_API = {"/api/auth/state", "/api/auth/login", "/api/auth/setup", "/healthz"}


# ---------------------------------------------------------------- password helpers
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


class Auth:
    def __init__(self, store: Store):
        self.store = store
        self.env_password = os.environ.get("WEB_PASSWORD") or None
        self.sessions: dict[str, float] = {}

    @property
    def password_set(self) -> bool:
        return bool(self.env_password or self.store.get_setting("web_password_hash"))

    def check(self, password: str) -> bool:
        if self.env_password:
            return hmac.compare_digest(password.encode(), self.env_password.encode())
        stored = self.store.get_setting("web_password_hash")
        return bool(stored and verify_password(password, stored))

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions[token] = time.time() + SESSION_TTL
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        exp = self.sessions.get(token)
        if not exp or exp < time.time():
            self.sessions.pop(token, None)
            return False
        return True


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def create_app(store: Store, manager: BotManager, activity: ActivityLog, env_token: str | None) -> web.Application:
    auth = Auth(store)

    @web.middleware
    async def auth_mw(request: web.Request, handler):
        path = request.path
        if path.startswith("/api/") and path not in PUBLIC_API:
            if not auth.valid(request.cookies.get(SESSION_COOKIE)):
                return _json_error(401, "Not logged in")
            # simple CSRF guard: state-changing calls must come from our own JS
            if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "relay-ui":
                return _json_error(403, "Missing CSRF header")
        try:
            return await handler(request)
        except RuleError as exc:
            return _json_error(400, str(exc))
        except discord.Forbidden as exc:
            return _json_error(403, f"Discord says the bot lacks permission: {exc.text}")
        except discord.NotFound:
            return _json_error(404, "Discord could not find that channel/message (is the bot in that server?)")

    app = web.Application(middlewares=[auth_mw], client_max_size=1024 * 1024)

    def client_or_409():
        c = manager.client
        if c is None or not c.is_ready():
            raise web.HTTPConflict(text='{"error":"The bot is not connected yet"}', content_type="application/json")
        return c

    def set_cookie(resp: web.Response, token: str, request: web.Request) -> None:
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="Strict",
                        secure=request.secure, path="/")

    # ------------------------------------------------------------ pages
    async def index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "bot": manager.status if not manager.client or not manager.client.is_ready() else "ready"})

    # ------------------------------------------------------------ auth
    async def auth_state(request: web.Request) -> web.Response:
        return web.json_response({"password_set": auth.password_set,
                                  "password_from_env": bool(auth.env_password),
                                  "logged_in": auth.valid(request.cookies.get(SESSION_COOKIE))})

    async def auth_setup(request: web.Request) -> web.Response:
        if auth.password_set:
            return _json_error(400, "A password is already set")
        body = await request.json()
        pw = str(body.get("password") or "")
        if len(pw) < 8:
            return _json_error(400, "Use at least 8 characters")
        store.set_setting("web_password_hash", hash_password(pw))
        resp = web.json_response({"ok": True})
        set_cookie(resp, auth.new_session(), request)
        return resp

    async def auth_login(request: web.Request) -> web.Response:
        body = await request.json()
        if not auth.check(str(body.get("password") or "")):
            await asyncio.sleep(1.0)  # slow down guessing
            return _json_error(401, "Wrong password")
        resp = web.json_response({"ok": True})
        set_cookie(resp, auth.new_session(), request)
        return resp

    async def auth_logout(request: web.Request) -> web.Response:
        auth.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        resp = web.json_response({"ok": True})
        resp.del_cookie(SESSION_COOKIE, path="/")
        return resp

    async def change_password(request: web.Request) -> web.Response:
        if auth.env_password:
            return _json_error(400, "The password is set by the WEB_PASSWORD environment variable; change it there")
        body = await request.json()
        if not auth.check(str(body.get("current") or "")):
            return _json_error(400, "Current password is wrong")
        new = str(body.get("new") or "")
        if len(new) < 8:
            return _json_error(400, "Use at least 8 characters")
        store.set_setting("web_password_hash", hash_password(new))
        return web.json_response({"ok": True})

    # ------------------------------------------------------------ bot
    async def status(request: web.Request) -> web.Response:
        data = manager.snapshot()
        data["token_set"] = bool(env_token or store.get_setting("discord_token"))
        data["token_from_env"] = bool(env_token)
        data["rule_count"] = len(store.list_rules())
        return web.json_response(data)

    async def set_token(request: web.Request) -> web.Response:
        if env_token:
            return _json_error(400, "The token is set by the DISCORD_TOKEN environment variable; change it there")
        body = await request.json()
        token = str(body.get("token") or "").strip()
        if token.lower().startswith("bot "):
            token = token[4:].strip()
        if len(token) < 50 or token.count(".") < 2:
            return _json_error(400, "That doesn't look like a bot token (Developer Portal → Bot → Reset Token)")
        store.set_setting("discord_token", token)
        await manager.start(token)
        return web.json_response({"ok": True})

    async def restart_bot(request: web.Request) -> web.Response:
        token = env_token or store.get_setting("discord_token")
        if not token:
            return _json_error(400, "No bot token configured")
        await manager.start(token)
        return web.json_response({"ok": True})

    async def guilds(request: web.Request) -> web.Response:
        return web.json_response(client_or_409().guild_snapshot())

    async def channel_authors(request: web.Request) -> web.Response:
        client = client_or_409()
        authors = await client.recent_authors(int(request.match_info["cid"]))
        return web.json_response(authors)

    async def leave_guild(request: web.Request) -> web.Response:
        client = client_or_409()
        g = client.get_guild(int(request.match_info["gid"]))
        if not g:
            return _json_error(404, "Unknown server")
        await g.leave()
        return web.json_response({"ok": True})

    # ------------------------------------------------------------ rules
    async def list_rules(request: web.Request) -> web.Response:
        return web.json_response(store.list_rules())

    async def create_rule(request: web.Request) -> web.Response:
        rule = normalize_rule(await request.json())
        return web.json_response(store.save_rule(rule), status=201)

    async def update_rule(request: web.Request) -> web.Response:
        rid = request.match_info["rid"]
        if not store.get_rule(rid):
            return _json_error(404, "Unknown rule")
        rule = normalize_rule(await request.json(), existing_id=rid)
        return web.json_response(store.save_rule(rule))

    async def delete_rule(request: web.Request) -> web.Response:
        if not store.delete_rule(request.match_info["rid"]):
            return _json_error(404, "Unknown rule")
        return web.json_response({"ok": True})

    async def test_rule(request: web.Request) -> web.Response:
        client = client_or_409()
        rule = store.get_rule(request.match_info["rid"])
        if not rule:
            return _json_error(404, "Unknown rule")
        return web.json_response(await client.send_test(rule))

    async def get_activity(request: web.Request) -> web.Response:
        include_filtered = request.query.get("filtered", "1") != "0"
        return web.json_response(activity.list(include_filtered=include_filtered))

    r = app.router
    r.add_get("/", index)
    r.add_get("/healthz", healthz)
    r.add_static("/static/", STATIC_DIR, append_version=True)
    r.add_get("/api/auth/state", auth_state)
    r.add_post("/api/auth/setup", auth_setup)
    r.add_post("/api/auth/login", auth_login)
    r.add_post("/api/auth/logout", auth_logout)
    r.add_post("/api/settings/password", change_password)
    r.add_post("/api/settings/token", set_token)
    r.add_get("/api/status", status)
    r.add_post("/api/bot/restart", restart_bot)
    r.add_get("/api/guilds", guilds)
    r.add_post("/api/guilds/{gid}/leave", leave_guild)
    r.add_get("/api/channels/{cid}/authors", channel_authors)
    r.add_get("/api/rules", list_rules)
    r.add_post("/api/rules", create_rule)
    r.add_put("/api/rules/{rid}", update_rule)
    r.add_delete("/api/rules/{rid}", delete_rule)
    r.add_post("/api/rules/{rid}/test", test_rule)
    r.add_get("/api/activity", get_activity)
    return app
