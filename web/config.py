"""Runtime configuration for the spotify-dl web UI."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_FORMATS: tuple[str, ...] = (
    "m4a",
    "mp3",
    "opus",
    "flac",
    "wav",
    "aac",
)

APP_NAME = "spotify-dl"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Immutable settings loaded from environment variables."""

    app_password: str
    secret_key: str
    data_dir: Path
    download_dir: Path
    log_dir: Path
    default_format: str
    default_concurrent: int
    max_concurrent: int
    session_max_age: int
    cookie_secure: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.db"


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _as_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    return max(minimum, min(maximum, value))


def _persisted_secret(data_dir: Path) -> str:
    path = data_dir / "secret_key"
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_urlsafe(48)
    path.write_text(key, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def load_settings() -> Settings:
    """Read settings from the environment, creating data directories."""

    password = os.environ.get("APP_PASSWORD", "")
    if not password:
        raise ConfigError(
            "APP_PASSWORD is not set. Set it in the environment (or in the "
            "stack's .env / secrets) before starting the web UI."
        )

    data_dir = Path(
        os.environ.get("DATA_DIR", "./data")
    ).expanduser().resolve()
    download_dir = Path(
        os.environ.get("DOWNLOAD_DIR", "./downloads")
    ).expanduser().resolve()

    log_dir = data_dir / "logs"
    for directory in (data_dir, download_dir, log_dir):
        directory.mkdir(parents=True, exist_ok=True)

    audio_format = os.environ.get("DEFAULT_AUDIO_FORMAT", "m4a").lower()
    if audio_format not in SUPPORTED_FORMATS:
        raise ConfigError(
            f"DEFAULT_AUDIO_FORMAT must be one of "
            f"{', '.join(SUPPORTED_FORMATS)}"
        )

    max_concurrent = _as_int("MAX_CONCURRENT", 8, 1, 32)
    default_concurrent = _as_int(
        "DEFAULT_CONCURRENT", 3, 1, max_concurrent
    )

    secret_key = os.environ.get("SECRET_KEY") or _persisted_secret(
        data_dir
    )

    return Settings(
        app_password=password,
        secret_key=secret_key,
        data_dir=data_dir,
        download_dir=download_dir,
        log_dir=log_dir,
        default_format=audio_format,
        default_concurrent=default_concurrent,
        max_concurrent=max_concurrent,
        session_max_age=_as_int("SESSION_MAX_AGE", 604800, 300, 2_592_000),
        cookie_secure=_as_bool(os.environ.get("COOKIE_SECURE")),
    )
