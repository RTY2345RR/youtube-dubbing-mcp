"""YouTube dubbing MCP server: tools an AI agent calls to dub a video, one cloned voice per speaker."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

from . import __version__, media, pipeline
from .client import MirageaClient, MirageaError
from .config import MISSING_KEY_MESSAGE, ConfigError, Settings, load_settings
from .project import Project, ProjectError, normalize_language
from .youtube import YouTubeError

try:  # official MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # official MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]
from mcp.types import ToolAnnotations

log = logging.getLogger("youtube_dubbing_mcp")

INSTRUCTIONS = """\
YouTube dubbing tools: dub a YouTube video, a local video/audio file or an SRT into another language, with one cloned voice per speaker, synced captions and upload-ready YouTube audio tracks. Miragea is the voice engine.
Workflow (each step saves to disk, so any step can be re-run):
1. dubbing_start_project(source=<YouTube URL | video/audio file | .srt/.vtt>) -> project_id, speakers, segments.
2. Check speakers: dubbing_get_segments (no language) shows who says what. Fix labels with dubbing_assign_speakers.
3. Voices: dubbing_extract_voice_samples(share=true) clones every speaker from the video itself, or dubbing_set_voice per speaker (saved actor_id from dubbing_list_voices, or a public reference_audio_url).
4. Translate: dubbing_get_segments(target_language=...) gives each line with target_chars/max_chars. YOU translate, then dubbing_set_translation(language, {id: text}, title, description). Shorten any too_long lines and set them again.
5. dubbing_render(language) -> dubbed audio track (length = video), SRT, preview MP4. Re-running resumes finished lines.
6. dubbing_export_youtube_package -> per-language folders for YouTube Studio > Languages (the YouTube API cannot upload audio tracks).
Never resubmit after a timeout: just call the same tool again.
"""

mcp = _Server(name="youtube-dubbing-mcp", instructions=INSTRUCTIONS)

# Test hooks
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
_SLEEP: Any = None
_SAMPLE_HTTP: Optional[httpx.AsyncClient] = None


def _annotations(read_only: bool, idempotent: bool = True, open_world: bool = True) -> ToolAnnotations:
    return ToolAnnotations.model_validate(
        {"readOnlyHint": read_only, "destructiveHint": False, "idempotentHint": idempotent, "openWorldHint": open_world}
    )


def _settings(need_key: bool = True) -> Settings:
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise ToolError(str(exc)) from None
    if need_key and not settings.has_key:
        raise ToolError(MISSING_KEY_MESSAGE)
    return settings


def _client_factory(settings: Settings):
    def make() -> MirageaClient:
        kwargs: dict[str, Any] = {"transport": _TRANSPORT}
        if _SLEEP is not None:
            kwargs["sleep"] = _SLEEP
        return MirageaClient(settings, **kwargs)

    return make


def _fail(exc: Exception) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, (MirageaError, ProjectError, YouTubeError, media.MediaError, ConfigError)):
        return ToolError(str(exc))
    if isinstance(exc, FileNotFoundError):
        return ToolError(f"File not found: {exc.filename or exc}")
    if isinstance(exc, (ValueError, OSError)):
        return ToolError(str(exc))
    return ToolError(f"Unexpected error: {type(exc).__name__}: {exc}")


def _load(settings: Settings, project_id: str) -> Project:
    try:
        return Project.load(settings.projects_dir, project_id)
    except ProjectError as exc:
        raise ToolError(str(exc)) from None


async def _progress(message: str) -> None:
    log.info(message)


# ===================================================================== tools
@mcp.tool(
    name="dubbing_check_setup",
    description=(
        "Check that dubbing will work: API key set (value hidden), voice engine reachable, its voice models and "
        "limits, ffmpeg and yt-dlp available, and where projects are saved. Read-only; creates no job. Call first."
    ),
    annotations=_annotations(read_only=True),
)
async def dubbing_check_setup() -> dict[str, Any]:
    settings = _settings(need_key=False)
    out: dict[str, Any] = {"version": __version__, "projects_folder": str(settings.projects_dir)}
    try:
        out["ffmpeg"] = media.ffmpeg_exe()
    except media.MediaError as exc:
        out["ffmpeg"] = f"missing: {exc}"
    try:
        import yt_dlp  # type: ignore[import-untyped]

        out["yt_dlp"] = getattr(getattr(yt_dlp, "version", None), "__version__", "installed")
    except ImportError:
        out["yt_dlp"] = "missing (pip install yt-dlp)"
    if not settings.has_key:
        out["api_key"] = "missing"
        out["next_step"] = MISSING_KEY_MESSAGE
        return out
    out["api_key"] = "set"
    try:
        async with _client_factory(settings)() as client:
            caps = await client.capabilities()
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    audio = caps.get("audio") or {}
    subs = caps.get("subtitles") or {}
    out["voice_engine"] = {
        "models": [
            {"id": m.get("id"), "uses_reference_text": m.get("uses_reference_text")}
            for m in audio.get("models") or []
            if isinstance(m, dict)
        ],
        "default_model": audio.get("default_model"),
        "voice_sources": audio.get("voice_sources"),
        "max_text_length": audio.get("max_text_length"),
        "transcription_max_minutes": subs.get("max_duration_minutes"),
        "transcription_max_upload_mb": subs.get("max_upload_mb"),
    }
    out["ready"] = True
    return out


@mcp.tool(
    name="dubbing_list_voices",
    description="List the voices saved in the user's Miragea account (actor_id, name, language) to use with dubbing_set_voice. Read-only.",
    annotations=_annotations(read_only=True),
)
async def dubbing_list_voices(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    settings = _settings()
    try:
        async with _client_factory(settings)() as client:
            return await client.list_actors(limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="dubbing_start_project",
    description=(
        "Start a dubbing project from a YouTube URL, a local video/audio file, or an SRT/VTT caption file. "
        "Downloads the audio (and optionally the video for a preview), takes the transcript from YouTube captions "
        "when available or transcribes the speech, merges caption fragments into sentences and labels speakers "
        "from caption markers (Name:, [Name], >>) or, without markers, by voice pitch (set num_speakers if known). Returns project_id, duration, speakers and segment count."
    ),
    annotations=_annotations(read_only=False, idempotent=False),
)
async def dubbing_start_project(
    source: str,
    source_language: Optional[str] = None,
    captions_file: Optional[str] = None,
    prefer_captions: bool = True,
    download_video: bool = False,
    num_speakers: Optional[int] = None,
    project_name: Optional[str] = None,
) -> dict[str, Any]:
    settings = _settings(need_key=False)
    source = (source or "").strip()
    if not source:
        raise ToolError("`source` is required: a YouTube URL, a video/audio file path or an SRT/VTT path.")
    kind = "youtube" if pipeline.youtube.is_youtube_url(source) else "file"
    name = project_name or (Path(source).stem if kind == "file" else "youtube-dub")
    try:
        project = Project.create(settings.projects_dir, name, source, kind)
        info = await pipeline.ingest(
            project,
            source,
            client_factory=_client_factory(_settings()) if settings.has_key else _no_key_factory,
            source_language=source_language,
            captions_file=captions_file,
            prefer_captions=prefer_captions,
            download_video=download_video,
            num_speakers=num_speakers,
            progress=_progress,
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    summary = project.summary()
    summary["speaker_detection"] = info["speaker_detection"]
    summary["next_step"] = (
        "Review speakers with dubbing_get_segments, then set voices (dubbing_extract_voice_samples share=true, "
        "or dubbing_set_voice)."
    )
    return summary


def _no_key_factory() -> MirageaClient:
    raise MirageaError("missing_api_key", MISSING_KEY_MESSAGE)


@mcp.tool(
    name="dubbing_get_segments",
    description=(
        "Read a project's transcript in pages: id, speaker, start, slot_seconds and text. With target_language, "
        "each line also gets target_chars and max_chars, the length that fits its time slot when spoken in that "
        "language, plus any translation already saved. Use it to review speakers and to translate."
    ),
    annotations=_annotations(read_only=True, open_world=False),
)
async def dubbing_get_segments(
    project_id: str, target_language: Optional[str] = None, offset: int = 0, limit: int = 80
) -> dict[str, Any]:
    settings = _settings(need_key=False)
    project = _load(settings, project_id)
    try:
        return pipeline.segment_rows(project, target_language, max(0, offset), max(1, min(400, limit)))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="dubbing_assign_speakers",
    description=(
        "Set who speaks each segment. `assignments` maps a segment id ('7'), an inclusive range ('7-12') or '*' "
        "(all) to a speaker name, e.g. {'1-4': 'Anna', '5': 'Ben'}. Use when captions had no speaker markers."
    ),
    annotations=_annotations(read_only=False, open_world=False),
)
async def dubbing_assign_speakers(project_id: str, assignments: dict[str, str]) -> dict[str, Any]:
    settings = _settings(need_key=False)
    project = _load(settings, project_id)
    try:
        return pipeline.assign_speakers(project, assignments)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="dubbing_extract_voice_samples",
    description=(
        "Cut a clean 4-14 second voice sample for each speaker from the source audio, with its transcript. "
        "With share=true each sample is uploaded to a temporary public file host (uguu.se, deleted after a few "
        "hours) so the voice engine can clone it, and the speaker's voice is set automatically. Ask the user "
        "before sharing someone's voice. With share=false the samples stay local."
    ),
    annotations=_annotations(read_only=False),
)
async def dubbing_extract_voice_samples(
    project_id: str, speakers: Optional[list[str]] = None, share: bool = False
) -> dict[str, Any]:
    settings = _settings(need_key=False)
    project = _load(settings, project_id)
    try:
        rows = await pipeline.extract_samples(project, speakers, share, _SAMPLE_HTTP)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    return {"samples": rows, "missing_voices": project.missing_voices()}


@mcp.tool(
    name="dubbing_set_voice",
    description=(
        "Choose the voice for one speaker: a saved voice (actor_id from dubbing_list_voices) or a voice to clone "
        "from a public reference_audio_url (6-15 s of clean speech) with its exact reference_text. Optional "
        "model ('expressive' or 'standard') and speed (0.25-4)."
    ),
    annotations=_annotations(read_only=False, open_world=False),
)
async def dubbing_set_voice(
    project_id: str,
    speaker: str,
    actor_id: Optional[str] = None,
    reference_audio_url: Optional[str] = None,
    reference_text: Optional[str] = None,
    model: Optional[str] = None,
    speed: Optional[float] = None,
) -> dict[str, Any]:
    settings = _settings(need_key=False)
    project = _load(settings, project_id)
    if speed is not None and not 0.25 <= speed <= 4:
        raise ToolError("speed must be between 0.25 and 4.")
    try:
        return pipeline.set_voice(
            project,
            speaker,
            {"actor_id": actor_id, "reference_audio_url": reference_audio_url, "reference_text": reference_text,
             "model": model, "speed": speed},
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="dubbing_set_translation",
    description=(
        "Save translated lines for a target language as {segment_id: text} (all at once or in batches; later "
        "calls update only the ids given), plus an optional localized YouTube title and description. Returns "
        "a fit check: lines that are too long for their time slot, and ids still missing."
    ),
    annotations=_annotations(read_only=False, open_world=False),
)
async def dubbing_set_translation(
    project_id: str,
    language: str,
    translations: dict[str, str],
    title: Optional[str] = None,
    description: Optional[str] = None,
    max_speedup: float = 1.25,
) -> dict[str, Any]:
    settings = _settings(need_key=False)
    project = _load(settings, project_id)
    try:
        return pipeline.set_translation(
            project, language, translations, title=title, description=description, max_speedup=max_speedup
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="dubbing_render",
    description=(
        "Voice every translated line with its speaker's voice, then build the dubbed audio track at the exact "
        "video length (each line starts with the original; lines are sped up only when needed, max_speedup "
        "1.0-1.5), the translated SRT, and a preview MP4 when the video was downloaded. Optional background: "
        "a music/effects file mixed under the voices. Re-running reuses finished lines and resumes pending ones."
    ),
    annotations=_annotations(read_only=False, idempotent=True),
)
async def dubbing_render(
    project_id: str,
    language: str,
    max_speedup: float = 1.25,
    model: Optional[str] = None,
    audio_format: Literal["m4a", "mp3", "wav"] = "m4a",
    background_file: Optional[str] = None,
    background_volume: float = 0.12,
    preview_video: bool = True,
) -> dict[str, Any]:
    settings = _settings()
    project = _load(settings, project_id)
    if not 1.0 <= max_speedup <= 1.5:
        raise ToolError("max_speedup must be between 1.0 and 1.5 (higher sounds rushed).")
    try:
        language = normalize_language(language)
        background = Path(background_file).expanduser() if background_file else None
        if background and not background.exists():
            raise ProjectError(f"background_file not found: {background}")
        async with _client_factory(settings)() as client:
            clips = await pipeline.synthesize_lines(
                project, language, client, model=model, concurrency=settings.concurrency, progress=_progress
            )
        return await asyncio.to_thread(
            pipeline.assemble,
            project,
            language,
            clips,
            max_speedup=max_speedup,
            out_format=audio_format,
            background=background,
            background_volume=background_volume,
            preview=preview_video,
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="dubbing_export_youtube_package",
    description=(
        "Write the YouTube upload package: one folder per language with audio.m4a (the dub track, video length), "
        "captions.srt, title.txt and description.txt, plus UPLOAD-TO-YOUTUBE.md with the YouTube Studio > "
        "Languages steps and a Studio link. The YouTube API cannot upload audio tracks, so the user uploads them."
    ),
    annotations=_annotations(read_only=False, open_world=False),
)
async def dubbing_export_youtube_package(project_id: str, languages: Optional[list[str]] = None) -> dict[str, Any]:
    settings = _settings(need_key=False)
    project = _load(settings, project_id)
    try:
        return pipeline.export_package(project, languages)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="dubbing_project_status",
    description="Show a project's state: speakers and their voices, languages translated and rendered, output paths.",
    annotations=_annotations(read_only=True, open_world=False),
)
async def dubbing_project_status(project_id: str) -> dict[str, Any]:
    settings = _settings(need_key=False)
    return _load(settings, project_id).summary()


@mcp.tool(
    name="dubbing_list_projects",
    description="List dubbing projects saved on this machine, newest first.",
    annotations=_annotations(read_only=True, open_world=False),
)
async def dubbing_list_projects() -> dict[str, Any]:
    settings = _settings(need_key=False)
    return {"projects_folder": str(settings.projects_dir), "projects": Project.list(settings.projects_dir)}


def run() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log.info("youtube-dubbing-mcp %s starting (stdio)", __version__)
    if not shutil.which("ffmpeg"):
        log.info("ffmpeg not on PATH; using the bundled imageio-ffmpeg binary")
    mcp.run("stdio")
