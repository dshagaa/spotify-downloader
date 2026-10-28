"""Validation helpers for user supplied playlist references."""

from __future__ import annotations

import re
from urllib.parse import urlparse

PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
SUBFOLDER_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
CANONICAL_URL = "https://open.spotify.com/playlist/{playlist_id}"


def normalize_playlist(raw: str) -> tuple[str, str] | None:
    """Return ``(canonical_url, playlist_id)`` or ``None`` if invalid."""

    value = raw.strip()

    if PLAYLIST_ID_RE.match(value):
        return CANONICAL_URL.format(playlist_id=value), value

    if value.startswith("spotify:playlist:"):
        playlist_id = value.rsplit(":", 1)[-1]
        if PLAYLIST_ID_RE.match(playlist_id):
            return CANONICAL_URL.format(playlist_id=playlist_id), playlist_id
        return None

    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc not in (
        "open.spotify.com",
        "play.spotify.com",
        "spotify.com",
        "www.spotify.com",
    ):
        return None

    parts = [part for part in parsed.path.split("/") if part]
    try:
        index = parts.index("playlist")
    except ValueError:
        return None
    if index + 1 >= len(parts):
        return None

    playlist_id = parts[index + 1]
    if not PLAYLIST_ID_RE.match(playlist_id):
        return None
    return CANONICAL_URL.format(playlist_id=playlist_id), playlist_id


def normalize_subfolder(raw: str | None) -> str:
    """Return a safe relative subfolder name, or ``""`` for invalid input."""

    value = (raw or "").strip()
    if not value:
        return ""
    if not SUBFOLDER_RE.match(value) or ".." in value:
        raise ValueError(
            "Subfolder may only contain letters, numbers, spaces, dots, "
            "dashes and underscores (max 64 characters)."
        )
    return value.strip(". ")
