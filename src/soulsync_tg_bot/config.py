import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(repr=False)
    allowed_ids: frozenset[int]
    soulsync_url: str
    soulsync_key: str = field(repr=False)
    database: Path
    search_source: str = "spotify"
    timeout: float = 20
    import_timeout: float = 60
    import_max_tracks: int = 1000
    poll_seconds: float = 2.5
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Config":
        def required(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise ValueError(f"Set {key}.")
            return value

        def number(key: str, default: str, minimum: float, maximum: float) -> float:
            try:
                value = float(env.get(key, default))
            except ValueError:
                raise ValueError(f"Invalid {key}.") from None
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(f"{key} must be between {minimum} and {maximum}.")
            return value

        ids = required("TELEGRAM_ALLOWED_USER_IDS").split(",")
        if any(not re.fullmatch(r"[0-9]{1,19}", item.strip()) for item in ids):
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain comma-separated numeric IDs.")
        allowed = frozenset(int(item) for item in ids)
        if any(not 0 < item < 2**63 for item in allowed):
            raise ValueError("Telegram user IDs must be positive integers.")
        url = required("SOULSYNC_URL").rstrip("/")
        try:
            parts = urlsplit(url)
            _ = parts.port
        except ValueError:
            raise ValueError("Invalid SOULSYNC_URL.") from None
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("SOULSYNC_URL must be an HTTP(S) base URL without credentials.")
        source = env.get("SOULSYNC_SEARCH_SOURCE", "spotify").strip().lower()
        if source not in {"spotify", "deezer", "itunes", "musicbrainz"}:
            raise ValueError("Invalid SOULSYNC_SEARCH_SOURCE.")
        timeout = number("SOULSYNC_HTTP_TIMEOUT_SECONDS", "20", 1, 300)
        max_tracks = number("SPOTIFY_IMPORT_MAX_TRACKS", "1000", 1, 5000)
        if not max_tracks.is_integer():
            raise ValueError("SPOTIFY_IMPORT_MAX_TRACKS must be an integer.")
        level = env.get("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid LOG_LEVEL.")
        return cls(
            required("TELEGRAM_BOT_TOKEN"),
            allowed,
            url,
            required("SOULSYNC_API_KEY"),
            Path(env.get("DATABASE_PATH", "./data/bot.sqlite3")),
            source,
            timeout,
            number("SOULSYNC_IMPORT_TIMEOUT_SECONDS", str(max(60, timeout)), timeout, 600),
            int(max_tracks),
            number("PIPELINE_POLL_SECONDS", "2.5", 1, 120),
            level,
        )
