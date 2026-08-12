"""Entry point for AIOud Trader."""

import asyncio
import json
import os
import signal
import sys

from aiohttp import web
from aiohttp.web_middlewares import middleware

from aioud.engine.runner import TradingEngine
from aioud.utils.config import Config
from aioud.utils import postgres_store
from aioud.utils.logger import get_logger, setup_logger

logger = get_logger(__name__)


async def main() -> int:
    """Run the autonomous trading bot."""
    config = Config.from_env()
    setup_logger(config.log_level)

    logger.info("Starting AIOud Trader | paper=%s", config.paper_trading)

    postgres_store.init_schema()

    engine = TradingEngine(config)

    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    async def state_handler(request: web.Request) -> web.Response:
        state = engine.get_state()
        return web.json_response(state)

    async def logs_handler(request: web.Request) -> web.Response:
        return web.json_response({"logs": engine.logs[-100:]})

    async def health_handler(request: web.Request) -> web.Response:
        """Lightweight health check for Render / load balancers."""
        return web.json_response({"status": "ok"})

    async def watchlist_remove_handler(request: web.Request) -> web.Response:
        body = await request.json()
        mint = body.get("mint", "")
        if not mint:
            return web.json_response({"ok": False, "error": "missing mint"}, status=400)
        removed = engine.remove_pair(mint)
        return web.json_response({"ok": removed})

    async def watchlist_add_handler(request: web.Request) -> web.Response:
        body = await request.json()
        mint = body.get("mint", "")
        if not mint:
            return web.json_response({"ok": False, "error": "missing mint"}, status=400)
        pair = await asyncio.to_thread(engine.add_pair, mint)
        if not pair:
            return web.json_response({"ok": False, "error": "token not found"}, status=404)
        return web.json_response({"ok": True, "symbol": pair.symbol, "mint": pair.mint})

    @middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            resp = web.Response()
        else:
            resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/api/state", state_handler)
    app.router.add_get("/api/logs", logs_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post("/api/watchlist/remove", watchlist_remove_handler)
    app.router.add_post("/api/watchlist/add", watchlist_add_handler)
    app.router.add_static("/", path=".", show_index=True)

    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Dashboard API running at http://%s:%s/mockup.html", host, port)

    await engine.start()

    loop_task = asyncio.create_task(engine.loop(scan_interval_minutes=1))

    # Catch unhandled exceptions in the trading loop so the process doesn't die silently.
    async def _watch_loop_task() -> None:
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Trading loop died unexpectedly: %s", exc)
            await engine.stop()
            raise

    asyncio.create_task(_watch_loop_task())

    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()
        await engine.stop()
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
