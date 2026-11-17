# Runs the model passthrough on :8080 and the guarded forward proxy on :3128 in one process.
from __future__ import annotations

import asyncio
import logging

import uvicorn

from egress import proxy
from egress.app import app


async def main() -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8080, access_log=False, log_level="info"))
    proxy_server = await proxy.serve()
    async with proxy_server:
        await server.serve()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(main())
