__version__ = "1.4.0"
__author__ = "Cha @github.com/invzfnc"

import concurrent.futures
import json
import re

from contextlib import ExitStack
from typing import Any, TypedDict
from time import sleep
from random import uniform
from sys import exit, stdout
from itertools import chain
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from spotapi import Public
from yt_dlp import YoutubeDL
from ytmusicapi import YTMusic

DOWNLOAD_PATH = "./downloads/"
AUDIO_FORMAT = "m4a"
CONCURRENT_LIMIT = 3

client = None


class PlaylistInfo(TypedDict):
    title: str
    artist: str


class TrackRecord(TypedDict):
    """A downloaded track, as written to the M3U8 playlist."""

    title: str
    artist: str
    path: str


def log(message: str = "") -> None:
    """Write a whole line at once so concurrent workers do not interleave."""

    stdout.write(f"{message}\n")


def resolve_archive_path(output_dir: str, archive: str) -> str:
    """Return the path yt-dlp should use for its download archive.

    Absolute paths are used as-is; relative ones are resolved inside the
    output directory (the historical CLI behaviour).
    """

    if archive.startswith(("/", "\\")) or Path(archive).is_absolute():
        return archive
    return str(Path(output_dir) / archive)


_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


def sanitize_name(name: str, fallback: str = "Playlist") -> str:
    """Turn a playlist title into a safe, portable folder name."""

    cleaned = _UNSAFE_FILENAME_CHARS.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = cleaned[:80].rstrip(" .")
    if not cleaned:
        return fallback
    if cleaned.upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def _playlist_id(value: str) -> str:
    """Extract a bare playlist ID from a URL, URI or ID."""

    value = value.strip()
    if "playlist/" in value:
        value = value.split("playlist/", 1)[1]
    elif value.startswith("spotify:playlist:"):
        value = value.split(":", 2)[2]
    return value.split("?", 1)[0].split("#", 1)[0].strip("/")


def get_playlist_name(playlist_url: str) -> str | None:
    """Return the public playlist title, or ``None`` when unavailable."""

    link = f"https://open.spotify.com/playlist/{_playlist_id(playlist_url)}"
    request = Request(
        f"https://open.spotify.com/oembed?{urlencode({'url': link})}",
        headers={"User-Agent": f"spotify-dl/{__version__}"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return None

    title = payload.get("title") if isinstance(payload, dict) else None
    if not isinstance(title, str):
        return None
    return title.strip() or None


def _video_id(url: str) -> str:
    """Return the YouTube video ID from a watch URL (or the last path part)."""

    if not url:
        return ""
    query = parse_qs(urlparse(url).query)
    values = query.get("v")
    if values:
        return values[0]
    return url.rsplit("/", 1)[-1]


def _display_value(info: dict[str, Any], *keys: str, default: str) -> str:
    """Return the first non-empty string field, collapsed to one line."""

    for key in keys:
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return default


def _archived_ids(archive: str | None) -> set[str]:
    """Return the IDs recorded in a yt-dlp download archive file."""

    if not archive:
        return set()
    try:
        text = Path(archive).read_text(encoding="utf-8")
    except OSError:
        return set()
    ids: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if parts:
            ids.add(parts[-1])
    return ids


def _final_path(filename: str | None, audio_format: str) -> Path | None:
    """Return the existing audio file for a yt-dlp download, if any."""

    if not filename:
        return None
    path = Path(filename)
    converted = path.with_suffix(f".{audio_format}")
    for candidate in (converted, path):
        if candidate.is_file():
            return candidate
    return None


def playlist_path(output_dir: str, playlist_name: str | None) -> Path:
    """Return the M3U8 path for a playlist folder."""

    base = Path(output_dir)
    name = sanitize_name(playlist_name or base.name or "playlist")
    return base / f"{name}.m3u8"


def write_playlist_file(output_dir: str, playlist_name: str | None,
                        tracks: list[TrackRecord]) -> Path | None:
    """Write an M3U8 playlist of the downloaded tracks, in playlist order."""

    if not tracks:
        return None

    base = Path(output_dir)
    target = playlist_path(output_dir, playlist_name)

    lines = ["#EXTM3U"]
    seen: set[str] = set()
    for track in tracks:
        path = Path(track["path"])
        if path.as_posix() in seen:
            continue
        seen.add(path.as_posix())

        try:
            relative = path.relative_to(base)
        except ValueError:
            relative = Path(path.name)

        label = track["title"]
        if track["artist"]:
            label = f"{track['artist']} - {label}"
        lines.append(f"#EXTINF:-1,{label}")
        lines.append(relative.as_posix())

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


AUDIO_EXTENSIONS = frozenset(
    ("m4a", "mp3", "opus", "flac", "wav", "aac", "ogg", "webm", "mkv")
)


def _audio_files(folder: Path) -> list[Path]:
    try:
        files = [
            path for path in folder.iterdir()
            if path.is_file()
            and path.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS
        ]
    except OSError:
        return []

    def modified(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(files, key=lambda path: (modified(path), path.name.lower()))


def rebuild_playlist(playlist_id: str, output_dir: str,
                     subfolder: str | None) -> Path | None:
    """Write the M3U8 for files that are already in the output folder.

    Existing playlists are left untouched. Returns the playlist path, or
    ``None`` when the folder has no downloaded audio files.
    """

    playlist_name = get_playlist_name(playlist_id)
    if subfolder is None:
        subfolder = sanitize_name(playlist_name or "")
    if subfolder:
        output_dir = str(Path(output_dir) / subfolder)

    folder = Path(output_dir)
    target = playlist_path(output_dir, playlist_name or folder.name)
    if target.exists():
        return target

    tracks: list[TrackRecord] = [
        {"title": path.stem, "artist": "", "path": str(path)}
        for path in _audio_files(folder)
    ]
    return write_playlist_file(
        output_dir, playlist_name or folder.name, tracks
    )


class _TrackingYDL(YoutubeDL):
    """YoutubeDL that remembers entries skipped by the download archive."""

    skipped_archive: list[dict[str, Any]]

    def __init__(self, params: Any = None, *args: Any,
                 **kwargs: Any) -> None:
        super().__init__(params, *args, **kwargs)
        self.skipped_archive = []

    def _match_entry(self, info_dict: dict[str, Any], *args: Any,
                     **kwargs: Any) -> Any:
        reason = super()._match_entry(info_dict, *args, **kwargs)
        if reason and "already been recorded in the archive" in reason:
            self.skipped_archive.append(dict(info_dict))
        return reason


def get_playlist_info(playlist_id: str) -> list[PlaylistInfo]:
    """Extracts data from Spotify and return them in format
       `[{"title": title, "artist": artist}]`."""

    result: list[PlaylistInfo] = []

    try:
        chunks = list(Public.playlist_info(playlist_id))
        items = list(chain.from_iterable([chunk["items"] for chunk in chunks]))
    except KeyError:
        return result

    for item in items:
        item = item["itemV2"]["data"]

        assert item["__typename"] in ("Track", "LocalTrack", "RestrictedContent", "NotFound", "Episode"), f"typename is {item['__typename']}"  # noqa: E501
        # RestrictedContent and NotFound:
        # Hidden entries, not actual songs in playlist

        song: PlaylistInfo

        if item["__typename"] == "Track":
            song = {
                "title": item["name"],
                "artist": item["artists"]["items"][0]["profile"]["name"],
            }
        elif item["__typename"] == "LocalTrack":
            song = {
                "title": item["name"],
                "artist": item["artistName"],
            }
        else:
            continue

        # remove duplicates
        if song in result:
            continue

        result.append(song)

    return result


def get_song_url(song_info: PlaylistInfo) -> tuple[str, str]:
    """Simulates searching from the YTMusic web and returns url to the
    closest match."""

    # setup client
    global client
    if client is None:
        client = YTMusic()
    data = client.search(f"{song_info['title']} {song_info['artist']}")

    # case where there are absolutely no results
    # ("no results for... try different keyword")
    if not data:
        # search only using title. opt for less specific searching
        data = client.search(f"{song_info['title']}")

    # worst case, still nothing. just give up.
    if not data:
        return ("", "")

    url_part = "https://music.youtube.com/watch?v="

    # observe: will "did you mean" interrupt the flow?

    # ignore top results and match entries from Song category
    songs = [entry for entry in data if entry["resultType"] == "song"]
    matches = [song for song in songs if song_info["title"] in song["title"]]
    # in the most ideal cases the titles are exactly the same,
    # but sometimes ytmusic has longer titles containing translations
    # for non english songs

    if matches:
        match = matches[0]
        return url_part + match["videoId"], match["title"]

    # if there is no Song result that exactly matches the given info,
    # return first Song or Video
    # removed top result because top result can sometimes contain albums
    streams = [
        entry for entry in data
        if entry["resultType"] in ("song", "video")
    ]
    if not streams:
        return ("", "")
    return url_part + streams[0]["videoId"], streams[0]["title"]


def get_song_urls(playlist_info: list[PlaylistInfo],
                  concurrent_limit: int) -> list[str]:
    """Repeatedly calls `get_song_url` on given playlist info.
    Returns list of results."""

    def process_song_entry(song_info: PlaylistInfo):
        """Helper function for concurrency in `get_song_urls`.
        Reports and prints status to user,
        returns matched url."""

        log(f"[MATCHING] {song_info['title']}")
        url, title = get_song_url(song_info)

        if url:
            log(f"[FOUND] {title} ({url})")
        else:
            log(f"[NO MATCH] {song_info['title']}")

        sleep(uniform(1, 2.5))

        return url

    urls: list[str] = []

    # split playlist_info into batches of (let's say) three
    # spltting manually so program responds better to keyboard interruption
    # program will end on ctrl-c once the batch is finished
    batches = [playlist_info[i: i+concurrent_limit]
               for i in range(0, len(playlist_info), concurrent_limit)]

    for batch in batches:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            # maintains order of urls with playlist entries
            urls.extend(executor.map(process_song_entry, batch))
        print()

    return urls


def download_from_urls(urls: list[str], output_dir: str,
                       audio_format: str, title_first: bool,
                       download_archive: str | None,
                       playlist_name: str | None = None) -> None:
    """Downloads list of songs with yt-dlp"""

    finished: dict[str, dict[str, Any]] = {}

    def report_download(status: dict) -> None:
        """Log a marker for each finished download (used by the web UI)."""

        if status.get("status") != "finished":
            return
        info = status.get("info_dict") or {}
        key = str(info.get("id") or _video_id(info.get("webpage_url", "")))
        finished[key] = dict(info)
        log(f"[DONE] {info.get('title') or 'track'}")

    if not output_dir.endswith("/"):
        output_dir += "/"

    if title_first:
        filename = f"{output_dir}%(title)s - %(creator)s.%(ext)s"
    else:
        filename = f"{output_dir}%(creator)s - %(title)s.%(ext)s"

    # options generated with https://github.com/yt-dlp/yt-dlp/blob/master/devscripts/cli_to_api.py  # noqa: E501
    options = {'concurrent_fragment_downloads': 3,
               'extract_flat': 'discard_in_playlist',
               'final_ext': 'm4a',
               'format': 'bestaudio/best',
               'fragment_retries': 10,
               'ignoreerrors': 'only_download',
               'outtmpl': {'default': filename,
                           'pl_thumbnail': ''},
               'postprocessor_args': {'ffmpeg': ['-c:v',
                                                 'mjpeg',
                                                 '-vf',
                                                 "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'"]},  # noqa: E501
               'postprocessors': [{'format': 'jpg',
                                   'key': 'FFmpegThumbnailsConvertor',
                                   'when': 'before_dl'},
                                  {'key': 'FFmpegExtractAudio',
                                   'nopostoverwrites': False,
                                   'preferredcodec': audio_format,
                                   'preferredquality': '5'},
                                  {'add_chapters': True,
                                   'add_infojson': 'if_exists',
                                   'add_metadata': True,
                                   'key': 'FFmpegMetadata'},
                                  {'already_have_thumbnail': False,
                                   'key': 'EmbedThumbnail'},
                                  {'key': 'FFmpegConcat',
                                   'only_multi_video': True,
                                   'when': 'playlist'}],
               'progress_hooks': [report_download],
               'retries': 10,
               'writethumbnail': True}

    # place in download folder
    if download_archive:
        options["download_archive"] = resolve_archive_path(
            output_dir, download_archive
        )

    # yt-dlp checks the archive before extracting metadata, so archived
    # tracks need a metadata-only lookup to recover their filenames.
    archive_file = options.get("download_archive")
    archived = _archived_ids(
        archive_file if isinstance(archive_file, str) else None
    )
    metadata_options = dict(options)
    metadata_options.pop("download_archive", None)
    metadata_options.pop("progress_hooks", None)
    metadata_options["quiet"] = True
    metadata_options["no_warnings"] = True

    # downloads stream with highest bitrate
    with ExitStack() as stack:
        ydl = stack.enter_context(_TrackingYDL(options))
        ydl.download(urls)

        skipped = {
            str(info.get("id") or _video_id(info.get("webpage_url", ""))): info
            for info in ydl.skipped_archive
        }
        metadata: YoutubeDL | None = None
        records: list[TrackRecord] = []
        for url in urls:
            video_id = _video_id(url)
            info = finished.get(video_id)
            prepared: str | None = None

            if info is not None:
                prepared = info.get("_filename") or info.get("filepath")
            elif video_id in skipped:
                info = skipped[video_id]
                try:
                    prepared = ydl.prepare_filename(info)
                except Exception:
                    prepared = None
            elif video_id in archived:
                if metadata is None:
                    metadata = stack.enter_context(
                        YoutubeDL(metadata_options)
                    )
                try:
                    info = metadata.extract_info(url, download=False)
                except Exception:
                    info = None
                if info:
                    try:
                        prepared = metadata.prepare_filename(info)
                    except Exception:
                        prepared = None
            else:
                continue

            path = _final_path(prepared, audio_format)
            if path is None or info is None:
                continue
            records.append({
                "title": _display_value(
                    info, "track", "title", default="track"
                ),
                "artist": _display_value(
                    info, "artist", "creator", "uploader", default=""
                ),
                "path": str(path),
            })

    playlist = write_playlist_file(output_dir, playlist_name, records)
    if playlist is not None:
        log(f"[PLAYLIST] {playlist}")


def main(playlist_id: str,
         output_dir: str,
         audio_format: str,
         title_first: bool,
         concurrent_limit: int,
         download_archive: str | None,
         subfolder: str | None = None) -> None:
    playlist_info = get_playlist_info(playlist_id)

    if not playlist_info:
        print("Invalid playlist URL. Aborting operation.")
        exit(1)

    playlist_name = get_playlist_name(playlist_id)

    if subfolder is None:
        subfolder = sanitize_name(playlist_name or "")

    if subfolder:
        output_dir = str(Path(output_dir) / subfolder)

    log(f"[TOTAL] {len(playlist_info)}")
    log(f"[OUTPUT] {output_dir}")

    download_urls = get_song_urls(playlist_info, concurrent_limit)
    download_from_urls(download_urls, output_dir, audio_format,
                       title_first, download_archive, playlist_name)


if __name__ == "__main__":
    url = "https://open.spotify.com/playlist/22hvxfJq0KwpgulLhDGslq"
    main(url, DOWNLOAD_PATH, AUDIO_FORMAT, False, CONCURRENT_LIMIT, ".archive")
