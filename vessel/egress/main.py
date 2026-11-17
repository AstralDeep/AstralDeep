# Runs the model passthrough on :8080 and the guarded forward proxy on :3128 in one process.
from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from egress import proxy
from egress.app import app


async def main() -> None:
    host = os.environ.get("EGRESS_HOST", "0.0.0.0")
    port, proxy_port = int(os.environ.get("EGRESS_PORT", "8080")), int(os.environ.get("EGRESS_PROXY_PORT", "3128"))
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, access_log=False, log_level="info"))
    proxy_server = await proxy.serve(host, proxy_port)
    async with proxy_server:
        await server.serve()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(main())
