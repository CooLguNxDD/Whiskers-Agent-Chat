"""One-command entrypoint. Always one worker: waiters live in this process."""

from __future__ import annotations

import uvicorn

from cat_fleet_chat.app import create_app
from cat_fleet_chat.config import assert_bind_safe, load_settings


def main() -> None:
    settings = load_settings()
    assert_bind_safe(settings)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
