"""YouTube access through yt-dlp: metadata, manual/automatic captions and audio.

The public YouTube Data API cannot upload extra audio tracks, so this module only
reads. Upload happens in YouTube Studio with the package `export_youtube_package` writes.
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path
from typing import Any, Optional

YOUTUBE_RE = re.compile(r"^(https?://)?([\w-]+\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/", re.I)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class YouTubeError(Exception):
    pass


def is_youtube_url(value: str) -> bool:
    return bool(YOUTUBE_RE.match(value.strip()))


def is_url(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://"))


def _ydl(opts: dict[str, Any]):
    try:
        import yt_dlp  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise YouTubeError("yt-dlp is not installed: pip install yt-dlp") from exc
    base: dict[str, Any] = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True}
    import os

    cookies = os.environ.get("YTDLP_COOKIES", "").strip()
    browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
    if cookies:
        base["cookiefile"] = os.path.expanduser(cookies)
    elif browser:
        base["cookiesfrombrowser"] = (browser,)
    base.update(opts)
    return yt_dlp.YoutubeDL(base)


def fetch_info(url: str) -> dict[str, Any]:
    """Video metadata (title, description, duration, language, caption tracks) without downloading."""
    try:
        with _ydl({"skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise YouTubeError(f"yt-dlp could not read {url}: {exc}{_hint(exc)}") from None
    if not info:
        raise YouTubeError(f"yt-dlp returned no metadata for {url}")
    return info


def _hint(exc: Exception) -> str:
    text = str(exc).lower()
    if "not a bot" in text or "sign in" in text or "cookies" in text:
        return (
            " | Hint: YouTube wants a signed-in browser session from this network. Set "
            "YTDLP_COOKIES_FROM_BROWSER=chrome (or firefox, edge, safari) in the server's env, or "
            "YTDLP_COOKIES=/path/to/cookies.txt. Or download the video yourself and pass the file path."
        )
    return ""


def pick_caption_track(info: dict[str, Any], language: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Best caption track: manual captions in `language` (or the video language), then automatic ones.

    Returns {"url", "ext", "language", "kind"} or None.
    """
    lang = (language or info.get("language") or "").lower() or None
    candidates: list[tuple[str, dict[str, Any]]] = [
        ("manual", info.get("subtitles") or {}),
        ("automatic", info.get("automatic_captions") or {}),
    ]
    for kind, tracks in candidates:
        if not tracks:
            continue
        keys = list(tracks)
        order: list[str] = []
        if lang:
            order += [k for k in keys if k.lower() == lang]
            order += [k for k in keys if k.lower().split("-")[0] == lang.split("-")[0] and k not in order]
            # automatic captions also list machine translations ("es-en"); prefer the "-orig" track
            order = sorted(order, key=lambda k: (0 if k.endswith("-orig") else 1, len(k)))
        elif kind == "automatic":
            order += [k for k in keys if k.endswith("-orig")]
        else:
            order += keys[:1]
        if not order and kind == "manual" and not lang:
            order = keys[:1]
        for key in order:
            formats = tracks.get(key) or []
            by_ext = {f.get("ext"): f for f in formats if f.get("url")}
            for ext in ("json3", "vtt", "srv3", "ttml", "srt"):
                if ext in by_ext and ext in ("json3", "vtt", "srt"):
                    return {"url": by_ext[ext]["url"], "ext": ext, "language": key.replace("-orig", ""), "kind": kind}
    return None


def download_text(url: str, timeout: float = 60.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - caption URL from yt-dlp
        return resp.read().decode("utf-8", "replace")


def download_audio(url: str, dest_dir: Path) -> Path:
    """Download the best audio-only stream (no re-encode). Returns the file path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = {"format": "bestaudio/best", "outtmpl": str(dest_dir / "source-audio.%(ext)s")}
    try:
        with _ydl(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except Exception as exc:
        raise YouTubeError(f"yt-dlp could not download audio from {url}: {exc}{_hint(exc)}") from None
    if not path.exists():
        found = sorted(dest_dir.glob("source-audio.*"))
        if not found:
            raise YouTubeError("yt-dlp finished but no audio file was written.")
        path = found[0]
    return path


def download_video(url: str, dest_dir: Path, max_height: int = 720) -> Path:
    """Download a progressive/merged MP4 for the local preview (optional)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/b[height<={max_height}][ext=mp4]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "source-video.%(ext)s"),
    }
    try:
        with _ydl(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    except Exception as exc:
        raise YouTubeError(f"yt-dlp could not download video from {url}: {exc}{_hint(exc)}") from None
    if not path.exists():
        found = sorted(dest_dir.glob("source-video.*"))
        if not found:
            raise YouTubeError("yt-dlp finished but no video file was written.")
        path = found[0]
    return path
