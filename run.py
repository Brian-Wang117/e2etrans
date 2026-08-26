"""Local development entry point: uvicorn on HOST/PORT from the environment."""

from __future__ import annotations

import logging

import uvicorn

from app.config import settings_from_env
from app.main import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main() -> None:
    settings = settings_from_env()
    app = create_app(settings)
    if settings.realtime.enabled:
        logging.getLogger(__name__).info(
            "realtime voice enabled (provider=%s, subtitles=%s)",
            settings.realtime.provider,
            "on" if settings.realtime.subtitle_enabled else "off",
        )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
