"""Entry point: starts the web UI and (if a token is known) the Discord bot in one event loop."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from aiohttp import web

from .relay import ActivityLog, BotManager
from .storage import Store
from .web import create_app


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    # this app never uses voice; hide the "PyNaCl/davey is not installed" warnings
    logging.getLogger("discord.client").addFilter(lambda r: "voice will NOT be supported" not in r.getMessage())
    log = logging.getLogger("relay")

    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    port = int(os.environ.get("WEB_PORT", "8080"))
    env_token = (os.environ.get("DISCORD_TOKEN") or "").strip() or None

    store = Store(data_dir / "relay.db")
    activity = ActivityLog()
    manager = BotManager(store, activity)

    app = create_app(store, manager, activity, env_token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Web UI listening on port %s (data in %s)", port, data_dir)

    token = env_token or store.get_setting("discord_token")
    if token:
        await manager.start(token)
    else:
        log.info("No bot token yet - open the web UI and add one under Settings")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows dev runs
            pass
    await stop.wait()
    log.info("Shutting down")
    await manager.stop()
    await runner.cleanup()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
