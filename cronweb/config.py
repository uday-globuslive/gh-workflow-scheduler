from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    secret_key: str
    admin_username: str
    admin_password: str
    database_url: str = "sqlite:///cronweb.db"
    github_api_url: str = "https://api.github.com"
    default_timezone: str = "UTC"
    host: str = "0.0.0.0"
    port: int = 8080
    scheduler_enabled: bool = True
    tick_seconds: int = 15
    misfire_grace_seconds: int = 3600
    secure_cookies: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        missing = [k for k in ("SECRET_KEY", "ADMIN_PASSWORD") if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        return cls(
            secret_key=os.environ["SECRET_KEY"],
            admin_username=os.environ.get("ADMIN_USERNAME", "admin"),
            admin_password=os.environ["ADMIN_PASSWORD"],
            database_url=os.environ.get("DATABASE_URL", cls.database_url),
            github_api_url=os.environ.get("GITHUB_API_URL", cls.github_api_url),
            default_timezone=os.environ.get("DEFAULT_TIMEZONE", cls.default_timezone),
            host=os.environ.get("HOST", cls.host),
            port=int(os.environ.get("PORT", cls.port)),
            scheduler_enabled=_bool(os.environ.get("SCHEDULER_ENABLED"), True),
            tick_seconds=int(os.environ.get("TICK_SECONDS", cls.tick_seconds)),
            misfire_grace_seconds=int(os.environ.get("MISFIRE_GRACE_SECONDS", cls.misfire_grace_seconds)),
            secure_cookies=_bool(os.environ.get("SECURE_COOKIES"), False),
        )
