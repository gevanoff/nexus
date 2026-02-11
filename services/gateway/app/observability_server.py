from __future__ import annotations

import os
import threading
from typing import Optional

import uvicorn

from app.config import S, logger
from app.observability_routes import router as observability_router
from fastapi import FastAPI


def _create_observability_app() -> FastAPI:
    app = FastAPI(title="Local AI Gateway Observability", version="0.1")
    app.include_router(observability_router)
    return app


class ObservabilityServer:
    def __init__(self) -> None:
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not getattr(S, "OBSERVABILITY_ENABLED", True):
            logger.info("observability: disabled")
            return

        if os.getenv("PYTEST_CURRENT_TEST"):
            logger.info("observability: skipped under pytest")
            return

        app = _create_observability_app()
        config = uvicorn.Config(
            app,
            host=S.OBSERVABILITY_HOST,
            port=S.OBSERVABILITY_PORT,
            log_level="info",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="gateway-observability", daemon=True)
        self._thread.start()
        logger.info("observability: started http://%s:%s", S.OBSERVABILITY_HOST, S.OBSERVABILITY_PORT)

    def stop(self) -> None:
        if not self._server:
            return
        self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("observability: stopped")
