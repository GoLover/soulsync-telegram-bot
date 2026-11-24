import logging
import sys

from telegram import Update
from telegram.error import TelegramError

from .app import build_application
from .config import Config
from .soulsync import SoulSyncError


def main() -> int:
    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=config.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # Telegram tokens are embedded in request URLs. Keep transport logs off
    # even under DEBUG and log only safe summaries at the app boundary.
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).disabled = True
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    try:
        app, _ = build_application(config)
        app.run_polling(
            allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], bootstrap_retries=0
        )
    except SoulSyncError as exc:
        print(f"SoulSync startup check failed. {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError, TelegramError) as exc:
        print(
            f"Bot startup failed ({type(exc).__name__}). Check configuration and connectivity.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
