"""The dubbing pipeline: ingest -> speakers -> voice samples -> translation -> render -> YouTube package.

The host agent (Claude, Cursor, Codex...) does the translating: it reads the
segments with their time budgets and writes the target-language lines back.
Everything that touches audio happens here, and the voices are rendered by the
Miragea voice engine (cloned from each speaker's sample, or a saved voice).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

from . import media, youtube
from .client import MirageaClient, MirageaError, new_idempotency_key
from .fit import base_lang, check_translation, slot_seconds, tempo_for
from .project import Project, ProjectError, normalize_language, speaker_slug, text_hash
from .subtitles import (
    Segment,
    detect_speakers,
    merge_cues,
    parse_any,
    renumber,
    resegment_recognition,
    to_srt,
)

CAPTION_EXTS = {".srt", ".vtt", ".json", ".json3"}
SAMPLE_MIN, SAMPLE_TARGET, SAMPLE_MAX = 4.0, 9.0, 14.0
SAMPLE_HOST = "https://uguu.se/upload"  # temporary public file host (files expire after a few hours)

ProgressFn = Callable[[str], Awaitable[None]]


async def _noop(_: str) -> None:
    return None


# ===================================================================== ingest
async def ingest(
    project: Project,
    source: str,
    *,
    client_factory: Callable[[], MirageaClient],
    source_language: Optional[str] = None,
    captions_file: Optional[str] = None,
    prefer_captions: bool = True,
    download_video: bool = False,
    num_speakers: Optional[int] = None,
    progress: ProgressFn = _noop,
) -> dict[str, Any]:
    """Fill the project with media and a timed, speaker-labelled transcript."""
    src_dir = project.path("source")
    segments: list[Segment] = []
    origin = None
    lang = normalize_language(source_language) if source_language else None

    if youtube.is_youtube_url(source):
        await progress("Reading YouTube metadata")
        info = await asyncio.to_thread(youtube.fetch_info, source)
        project.data.update(
            title=info.get("title") or project.data["title"],
            description=(info.get("description") or "")[:5000],
            duration=info.get("duration"),
            youtube={
                "id": info.get("id"),
                "channel": info.get("channel") or info.get("uploader"),
                "url": info.get("webpage_url") or source,
                "chapters": [
                    {"title": c.get("title"), "start": c.get("start_time")} for c in info.get("chapters") or []
                ],
            },
        )
        lang = lang or (normalize_language(info["language"]) if info.get("language") else None)
        await progress("Downloading the audio track")
        audio = await asyncio.to_thread(youtube.download_audio, source, src_dir)
        project.set_media("audio", audio)
        if download_video:
            await progress("Downloading the video for the preview")
            video = await asyncio.to_thread(youtube.download_video, source, src_dir)
            project.set_media("video", video)
        if prefer_captions and not captions_file:
            track = youtube.pick_caption_track(info, lang)
            if track:
                text = await asyncio.to_thread(youtube.download_text, track["url"])
                (src_dir / f"captions.{track['language']}.{track['ext']}").write_text(text, encoding="utf-8")
                segments = parse_any(text, track["ext"])
                origin = f"youtube-{track['kind']}-captions"
                lang = lang or normalize_language(track["language"])
    else:
        path = Path(source).expanduser()
        if not path.exists():
            raise ProjectError(f"Source not found: {source}. Give a YouTube URL, a video/audio file or an SRT/VTT file.")
        if path.suffix.lower() in CAPTION_EXTS:
            captions_file = captions_file or str(path)
        elif media.is_media(path):
            dest = src_dir / ("source-video" if media.is_video(path) else "source-audio")
            dest = dest.with_suffix(path.suffix.lower())
            await asyncio.to_thread(shutil.copyfile, path, dest)
            project.set_media("video" if media.is_video(path) else "audio", dest)
            if media.is_video(path):
                project.set_media("audio", dest)
            project.data["duration"] = await asyncio.to_thread(media.probe_duration, dest)
        else:
            raise ProjectError(f"Unsupported source type {path.suffix!r}.")

    if captions_file:
        cpath = Path(captions_file).expanduser()
        text = cpath.read_text(encoding="utf-8-sig")
        shutil.copyfile(cpath, src_dir / f"captions{cpath.suffix.lower()}")
        segments = parse_any(text, cpath.suffix)
        origin = "caption-file"

    if not segments:
        audio = project.media_path("audio")
        if not audio:
            raise ProjectError("No captions and no audio to transcribe.")
        await progress("Transcribing speech with the Miragea subtitle engine")
        segments = await transcribe(project, audio, client_factory, progress)
        origin = "speech-recognition"

    if not segments:
        raise ProjectError("The transcript is empty: no speech or captions were found.")
    segments = [s for s in segments if re.sub(r"\[[^\]]*\]|♪", "", s.text).strip(" .-")]
    if not segments:
        raise ProjectError("The transcript only contains sound tags like [Music]; there is no speech to dub.")
    segments = merge_cues(segments)
    speakers = detect_speakers(segments)
    audio = project.media_path("audio")
    if speakers["method"] == "none" and audio and (num_speakers is None or num_speakers > 1):
        await progress("Grouping lines by voice")
        speakers = await asyncio.to_thread(group_by_voice, segments, audio, num_speakers)
    renumber(segments)
    project.set_segments(segments)
    project.data["source_language"] = lang or project.data.get("source_language")
    project.data["transcript_origin"] = origin
    if not project.data.get("duration"):
        project.data["duration"] = round(segments[-1].end + 1.0, 3)
    project.save()
    return {"speaker_detection": speakers}


async def transcribe(
    project: Project, audio: Path, client_factory: Callable[[], MirageaClient], progress: ProgressFn
) -> list[Segment]:
    job = (project.data.get("jobs") or {}).get("transcript") or {}
    mp3 = project.path("source", "speech-16k.mp3")
    if not mp3.exists():
        await asyncio.to_thread(media.extract_audio, audio, mp3)
    async with client_factory() as client:
        if not job.get("task_id"):
            caps = await client.capabilities()
            rate = (caps.get("subtitles") or {}).get("points_per_second")
            key = job.get("idempotency_key") or new_idempotency_key()
            project.data.setdefault("jobs", {})["transcript"] = {"idempotency_key": key}
            project.save()
            task = await client.create_recognition(mp3, rate, key)
            project.data["jobs"]["transcript"] = {"idempotency_key": key, "task_id": task["task_id"]}
            project.save()
            job = project.data["jobs"]["transcript"]
        task = await client.wait_for_task(job["task_id"], progress=progress)
    if task.get("status") != "succeeded":
        project.data["jobs"].pop("transcript", None)
        project.save()
        err = task.get("error") or {}
        raise MirageaError(str(err.get("code") or "task_failed"), str(err.get("message") or "Transcription failed."))
    result = task.get("result") or {}
    (project.path("source", "transcript.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    silences = await asyncio.to_thread(media.detect_silences, audio)
    if result.get("language") and not project.data.get("source_language"):
        try:
            project.data["source_language"] = normalize_language(str(result["language"]))
        except ProjectError:
            pass
    return resegment_recognition(result, silences)


def group_by_voice(segments: list[Segment], audio: Path, num_speakers: Optional[int] = None) -> dict[str, Any]:
    """Label speakers by voice pitch when captions carry no names (2+ clearly different voices).

    Each segment's median pitch is measured on up to 3 s of its middle, then the
    log-pitches are split into `num_speakers` groups (auto: 2 groups when the two
    clusters differ by more than a third, else 1). Always review the result.
    """
    import math

    pitches: list[Optional[float]] = []
    for seg in segments:
        mid = (seg.start + seg.end) / 2
        half = min(1.5, seg.duration / 2)
        pitches.append(media.median_pitch(audio, mid - half, mid + half))
    known = sorted(math.log(p) for p in pitches if p)
    k = num_speakers or 2
    if len(known) < 2 or k < 2:
        for seg in segments:
            seg.speaker = "Speaker 1"
        return {"speakers": ["Speaker 1"], "method": "single-voice", "changes": 0}
    # 1-D k-means on log pitch
    centers = [known[int(i * (len(known) - 1) / (k - 1))] for i in range(k)]
    for _ in range(25):
        groups: list[list[float]] = [[] for _ in centers]
        for v in known:
            groups[min(range(k), key=lambda c: abs(v - centers[c]))].append(v)
        centers = [sum(g) / len(g) if g else centers[i] for i, g in enumerate(groups)]
    centers.sort()
    if num_speakers is None and (len(centers) < 2 or math.exp(centers[-1] - centers[0]) < 1.33):
        for seg in segments:
            seg.speaker = "Speaker 1"
        return {"speakers": ["Speaker 1"], "method": "single-voice", "changes": 0}
    previous = "Speaker 1"
    for seg, p in zip(segments, pitches):
        if p:
            idx = min(range(len(centers)), key=lambda c: abs(math.log(p) - centers[c]))
            previous = f"Speaker {idx + 1}"
        seg.speaker = previous
    names: list[str] = []
    for seg in segments:
        if seg.speaker not in names:
            names.append(seg.speaker or "Speaker 1")
    changes = sum(1 for a, b in zip(segments, segments[1:]) if a.speaker != b.speaker)
    return {
        "speakers": names,
        "method": "voice-pitch",
        "changes": changes,
        "pitch_hz": {f"Speaker {i + 1}": round(math.exp(c)) for i, c in enumerate(centers)},
        "note": "Grouped by voice pitch (lowest = Speaker 1). Review with dubbing_get_segments and fix with dubbing_assign_speakers.",
    }


# =================================================================== speakers
def assign_speakers(project: Project, assignments: dict[str, str]) -> dict[str, Any]:
    """`{"3": "Anna", "5-9": "Ben", "*": "Host"}`: ids, inclusive ranges or every segment."""
    segments = project.segments
    by_id = {s.id: s for s in segments}
    changed = 0
    for key, name in assignments.items():
        name = (name or "").strip()
        if not name or len(name) > 60:
            raise ProjectError(f"Invalid speaker name for {key!r}.")
        key = str(key).strip()
        if key == "*":
            ids = list(by_id)
        elif re.fullmatch(r"\d+", key):
            ids = [int(key)]
        elif re.fullmatch(r"\d+\s*-\s*\d+", key):
            a, b = (int(x) for x in key.split("-"))
            ids = list(range(min(a, b), max(a, b) + 1))
        else:
            raise ProjectError(f"Segment key {key!r} must be an id (7), a range (7-12) or '*'.")
        for i in ids:
            if i not in by_id:
                raise ProjectError(f"Segment {i} does not exist (1..{len(segments)}).")
            if by_id[i].speaker != name:
                by_id[i].speaker = name
                changed += 1
    project.set_segments(segments)
    used = set(project.speaker_names())
    for name in list(project.data["speakers"]):
        if name not in used and not project.data["speakers"][name]:
            del project.data["speakers"][name]
    project.save()
    return {"changed_segments": changed, "speakers": project.speaker_names()}


def set_voice(project: Project, speaker: str, voice: dict[str, Any]) -> dict[str, Any]:
    if speaker not in project.speaker_names():
        raise ProjectError(f"Unknown speaker {speaker!r}. Speakers: {', '.join(project.speaker_names())}.")
    clean = {k: v for k, v in voice.items() if v not in (None, "")}
    if bool(clean.get("actor_id")) == bool(clean.get("reference_audio_url")):
        raise ProjectError("Give exactly one of actor_id (a saved voice) or reference_audio_url (a clip to clone).")
    url = clean.get("reference_audio_url")
    if url and not str(url).lower().startswith(("http://", "https://")):
        raise ProjectError(
            "reference_audio_url must be a public http(s) URL the voice engine can download. "
            "For a local file, call dubbing_extract_voice_samples with share=true."
        )
    project.data["speakers"][speaker] = clean
    project.save()
    return {"speaker": speaker, "voice": clean, "missing_voices": project.missing_voices()}


# ============================================================== voice samples
def choose_sample_spans(segments: list[Segment], speaker: str) -> list[Segment]:
    """Consecutive segments of one speaker totalling ~SAMPLE_TARGET seconds, longest clean run first."""
    runs: list[list[Segment]] = []
    current: list[Segment] = []
    for seg in segments:
        if seg.speaker == speaker and (not current or seg.start - current[-1].end < 0.8):
            current.append(seg)
        else:
            if current:
                runs.append(current)
            current = [seg] if seg.speaker == speaker else []
    if current:
        runs.append(current)
    best: list[Segment] = []
    best_score = -1.0
    for run in runs:
        for i in range(len(run)):
            picked: list[Segment] = []
            for seg in run[i:]:
                if picked and seg.end - picked[0].start > SAMPLE_MAX:
                    break
                picked.append(seg)
                if seg.end - picked[0].start >= SAMPLE_TARGET:
                    break
            if not picked:
                continue
            length = picked[-1].end - picked[0].start
            score = min(length, SAMPLE_TARGET) - max(0.0, length - SAMPLE_TARGET) * 0.5
            if score > best_score:
                best, best_score = picked, score
    return best


async def extract_samples(
    project: Project, speakers: Optional[list[str]] = None, share: bool = False, http: Optional[httpx.AsyncClient] = None
) -> list[dict[str, Any]]:
    audio = project.media_path("audio")
    if not audio:
        raise ProjectError(
            "This project has no source audio (it was started from a caption file). "
            "Set each speaker's voice with dubbing_set_voice instead."
        )
    names = speakers or project.speaker_names()
    out = []
    for name in names:
        spans = choose_sample_spans(project.segments, name)
        if not spans:
            out.append({"speaker": name, "error": "no segments for this speaker"})
            continue
        start, end = max(0.0, spans[0].start - 0.05), spans[-1].end + 0.15
        dest = project.path("samples", f"{speaker_slug(name)}.wav")
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            media.run_ffmpeg,
            ["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(audio), "-ac", "1", "-ar", "24000", str(dest)],
        )
        reference_text = " ".join(s.text for s in spans)
        row: dict[str, Any] = {
            "speaker": name,
            "sample": str(dest),
            "seconds": round(end - start, 2),
            "reference_text": reference_text,
            "short": (end - start) < SAMPLE_MIN,
        }
        if share:
            url = await upload_sample(dest, http)
            row["reference_audio_url"] = url
            set_voice(project, name, {"reference_audio_url": url, "reference_text": reference_text})
        else:
            current = project.voice(name)
            current["sample"] = str(dest)
            current["sample_text"] = reference_text
            project.data["speakers"][name] = current
            project.save()
        out.append(row)
    return out


async def upload_sample(path: Path, http: Optional[httpx.AsyncClient] = None) -> str:
    """Upload a sample to a temporary public host so the voice engine can fetch it."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=120, headers={"User-Agent": "youtube-dubbing-mcp"})
    try:
        with path.open("rb") as fh:
            resp = await http.post(SAMPLE_HOST, files={"files[]": (path.name, fh, "audio/wav")})
        resp.raise_for_status()
        data = resp.json()
        url = (data.get("files") or [{}])[0].get("url")
        if not data.get("success") or not url:
            raise ValueError("unexpected response")
        return str(url)
    except (httpx.HTTPError, ValueError) as exc:
        raise ProjectError(
            f"Could not upload the voice sample ({type(exc).__name__}). Host {path} yourself "
            "(any public https URL) and pass it to dubbing_set_voice."
        ) from None
    finally:
        if own:
            await http.aclose()


# ================================================================ translation
def set_translation(
    project: Project,
    language: str,
    lines: dict[str, str],
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    max_speedup: float = 1.25,
) -> dict[str, Any]:
    language = normalize_language(language)
    valid = {s.id for s in project.segments}
    current = {str(k): v for k, v in project.translation(language).items()}
    for key, text in lines.items():
        try:
            sid = int(str(key).strip())
        except ValueError:
            raise ProjectError(f"Translation keys must be segment ids, got {key!r}.") from None
        if sid not in valid:
            raise ProjectError(f"Segment {sid} does not exist (1..{len(valid)}).")
        current[str(sid)] = re.sub(r"\s+", " ", str(text or "")).strip()
    project.data.setdefault("translations", {})[language] = dict(sorted(current.items(), key=lambda kv: int(kv[0])))
    meta = project.data.setdefault("metadata", {}).setdefault(language, {})
    if title:
        meta["title"] = title.strip()[:100]
    if description:
        meta["description"] = description.strip()[:5000]
    project.save()
    report = check_translation(project.segments, project.translation(language), language, project.duration(), max_speedup)
    report["title"] = meta.get("title")
    return report


# ===================================================================== render
def _voice_payload(voice: dict[str, Any], text: str, model: Optional[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": text, "format": "wav"}
    if voice.get("actor_id"):
        payload["actor_id"] = str(voice["actor_id"])
    else:
        payload["reference_audio_url"] = voice["reference_audio_url"]
        if voice.get("reference_text"):
            payload["reference_text"] = voice["reference_text"]
    chosen = model or voice.get("model")
    if chosen:
        payload["model"] = chosen
    if voice.get("speed"):
        payload["speed"] = voice["speed"]
    return payload


async def synthesize_lines(
    project: Project,
    language: str,
    client: MirageaClient,
    *,
    model: Optional[str] = None,
    concurrency: int = 4,
    progress: ProgressFn = _noop,
) -> dict[int, Path]:
    """One clip per segment. Saved task ids make a re-run resume instead of resubmitting."""
    segments = project.segments
    tr = project.translation(language)
    missing = [s.id for s in segments if not (tr.get(s.id) or "").strip()]
    if missing:
        raise ProjectError(f"{len(missing)} segments have no {language} line yet (ids {missing[:15]}). Call dubbing_set_translation.")
    if project.missing_voices():
        raise ProjectError(
            "No voice for: " + ", ".join(project.missing_voices())
            + ". Call dubbing_extract_voice_samples(share=true) or dubbing_set_voice."
        )
    jobs = project.data.setdefault("jobs", {}).setdefault(language, {})
    lines_dir = project.path("lines", language)
    lines_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, concurrency))
    lock = asyncio.Lock()
    done: dict[int, Path] = {}

    async def one(seg: Segment) -> None:
        payload = _voice_payload(project.voice(seg.speaker or ""), tr[seg.id], model)
        fingerprint = text_hash(payload)
        dest = lines_dir / f"{seg.id:04d}.wav"
        job = jobs.get(str(seg.id)) or {}
        if job.get("fingerprint") == fingerprint and job.get("file") and dest.exists():
            done[seg.id] = dest
            return
        if job.get("fingerprint") != fingerprint:
            job = {"fingerprint": fingerprint, "idempotency_key": new_idempotency_key()}
            dest.unlink(missing_ok=True)
        async with sem:
            if not job.get("task_id"):
                async with lock:
                    jobs[str(seg.id)] = job
                    project.save()
                task = await client.create_audio(payload, job["idempotency_key"])
                job["task_id"] = task["task_id"]
                async with lock:
                    project.save()
            task = await client.wait_for_task(job["task_id"])
            if task.get("status") != "succeeded":
                err = task.get("error") or {}
                async with lock:
                    jobs.pop(str(seg.id), None)
                    project.save()
                raise MirageaError(
                    str(err.get("code") or "task_failed"),
                    f"Segment {seg.id}: {err.get('message') or 'voice generation failed'}",
                    retryable=bool(err.get("retryable")),
                    hint="Re-run dubbing_render to retry this line. If it keeps failing, check the voice sample URL.",
                )
            url = (task.get("result") or {}).get("audio_url")
            if not url:
                raise MirageaError("invalid_result", f"Segment {seg.id}: the task returned no audio_url.")
            saved = await client.download(url, dest.with_suffix(".part.wav"))
            saved.replace(dest)
            job["file"] = dest.name
            async with lock:
                project.save()
            done[seg.id] = dest
            await progress(f"Voiced {len(done)}/{len(segments)} lines")

    results = await asyncio.gather(*(one(s) for s in segments), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        first = errors[0]
        if isinstance(first, MirageaError):
            first.message = f"{len(errors)} of {len(segments)} lines failed; finished lines are kept. " + first.message
        raise first
    return done


def assemble(
    project: Project,
    language: str,
    clips: dict[int, Path],
    *,
    max_speedup: float = 1.25,
    out_format: str = "m4a",
    background: Optional[Path] = None,
    background_volume: float = 0.12,
    preview: bool = True,
) -> dict[str, Any]:
    """Place every clip at its segment's start, speeding up (pitch kept) only when it would overlap."""
    segments = project.segments
    tr = project.translation(language)
    duration = project.duration()
    out_dir = project.path("dub", language)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = out_dir / f"voice-{language}.wav"
    writer = media.WavWriter(wav)
    timed: list[Segment] = []
    report = []
    cursor = 0.0
    for i, seg in enumerate(segments):
        slot = slot_seconds(segments, i, duration)
        raw = media.decode_pcm(clips[seg.id])
        seconds = media.pcm_seconds(raw)
        start = max(seg.start, cursor)
        available = max(0.3, slot - (start - seg.start))
        tempo, overflow = tempo_for(seconds, available, max_speedup)
        pcm = media.decode_pcm(clips[seg.id], tempo=tempo) if tempo != 1.0 else raw
        writer.pad_to(start)
        writer.pcm(pcm)
        cursor = writer.seconds + 0.08
        placed = media.pcm_seconds(pcm)
        timed.append(Segment(seg.id, start, start + placed, tr[seg.id], seg.speaker))
        report.append(
            {
                "id": seg.id,
                "speaker": seg.speaker,
                "start": round(start, 2),
                "seconds": round(placed, 2),
                "slot": round(slot, 2),
                "tempo": tempo,
                "overflow": overflow,
                "drift": round(start - seg.start, 2),
            }
        )
    if duration:
        writer.pad_to(duration)
    writer.close()

    title = project.data.get("title") or project.id
    stem = f"{re.sub(r'[^A-Za-z0-9]+', '-', title).strip('-')[:50] or 'dub'}.{language}"
    track = media.encode_track(wav, out_dir / f"{stem}.{out_format}", background=background, background_volume=background_volume)
    srt = out_dir / f"{stem}.srt"
    srt.write_text(to_srt(timed), encoding="utf-8")
    speakers_srt = out_dir / f"{stem}.speakers.srt"
    speakers_srt.write_text(to_srt(timed, with_speaker=True), encoding="utf-8")
    video_out = None
    video = project.media_path("video")
    if preview and video and media.has_video_stream(video):
        video_out = media.mux_preview(video, track, out_dir / f"{stem}.preview.mp4", srt)
    overflowing = [r for r in report if r["overflow"] > 0.05]
    sped = [r for r in report if r["tempo"] > 1.0]
    result = {
        "language": language,
        "track": str(track),
        "captions": str(srt),
        "captions_with_speakers": str(speakers_srt),
        "preview_video": str(video_out) if video_out else None,
        "track_seconds": round(media.probe_duration(track), 2),
        "video_seconds": round(duration or 0, 2),
        "lines": len(report),
        "sped_up_lines": len(sped),
        "max_tempo": max([r["tempo"] for r in report] or [1.0]),
        "overflowing_lines": overflowing[:20],
        "advice": (
            "Timing is clean."
            if not overflowing
            else "Some lines are still too long for their slot even when sped up. Shorten them with "
            "dubbing_set_translation (only those ids) and run dubbing_render again; unchanged lines are reused."
        ),
    }
    project.data.setdefault("renders", {})[language] = {
        "track": str(track),
        "captions": str(srt),
        "preview_video": result["preview_video"],
        "timing": report,
    }
    project.save()
    wav.unlink(missing_ok=True)
    return result


# ==================================================================== export
YOUTUBE_STEPS = """\
# Upload these dubs to YouTube

YouTube's public Data API cannot add audio tracks, so this last step happens in
YouTube Studio (about one minute per language). Multi-language audio is available
to channels that YouTube has enabled for it.

1. Open https://studio.youtube.com, choose **Content**, then the video.
2. Open **Languages** in the left menu. Set the video language if it is not set yet.
3. For each folder below, click **Add language**, choose the language, then:
   - **Dub** -> **Add** -> upload `audio.<ext>` from that folder -> **Publish**.
   - **Subtitles** -> **Add** -> **Upload file** -> *With timing* -> `captions.srt`.
   - **Title & description** -> **Add** -> paste `title.txt` and `description.txt` (when present).
4. Viewers pick the track under **Settings (gear) -> Audio track**.

Audio tracks must match the video length (these do) and contain only the dub.
"""


def export_package(project: Project, languages: Optional[list[str]] = None) -> dict[str, Any]:
    renders = project.data.get("renders") or {}
    langs = [normalize_language(x) for x in languages] if languages else sorted(renders)
    if not langs:
        raise ProjectError("Nothing rendered yet. Call dubbing_render first.")
    root = project.path("youtube-upload")
    root.mkdir(exist_ok=True)
    rows = []
    for lang in langs:
        render = renders.get(lang)
        if not render or not Path(render["track"]).exists():
            raise ProjectError(f"No rendered {lang} track. Call dubbing_render(language='{lang}').")
        folder = root / lang
        folder.mkdir(exist_ok=True)
        track = Path(render["track"])
        shutil.copyfile(track, folder / f"audio{track.suffix}")
        shutil.copyfile(render["captions"], folder / "captions.srt")
        meta = (project.data.get("metadata") or {}).get(lang) or {}
        if meta.get("title"):
            (folder / "title.txt").write_text(meta["title"] + "\n", encoding="utf-8")
        if meta.get("description"):
            (folder / "description.txt").write_text(meta["description"] + "\n", encoding="utf-8")
        rows.append({"language": lang, "folder": str(folder), "audio": str(folder / f"audio{track.suffix}"),
                     "has_title": bool(meta.get("title")), "has_description": bool(meta.get("description"))})
    yt = project.data.get("youtube") or {}
    manifest = {
        "project_id": project.id,
        "video": yt.get("url") or project.data.get("source"),
        "source_language": project.data.get("source_language"),
        "languages": rows,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "UPLOAD-TO-YOUTUBE.md").write_text(YOUTUBE_STEPS, encoding="utf-8")
    return {"folder": str(root), "languages": rows, "instructions": str(root / "UPLOAD-TO-YOUTUBE.md"),
            "studio_url": f"https://studio.youtube.com/video/{yt['id']}/translations" if yt.get("id") else "https://studio.youtube.com"}


def segment_rows(project: Project, language: Optional[str], offset: int, limit: int, max_speedup: float = 1.25) -> dict[str, Any]:
    from .fit import budgets

    segments = project.segments
    lang = normalize_language(language) if language else None
    rows = budgets(segments, lang or "en", project.duration(), max_speedup)
    tr = project.translation(lang) if lang else {}
    page = rows[offset : offset + limit]
    for row in page:
        if not lang:
            row.pop("target_chars")
            row.pop("max_chars")
        elif tr.get(row["id"]):
            row["translation"] = tr[row["id"]]
    return {
        "project_id": project.id,
        "source_language": project.data.get("source_language"),
        "target_language": lang,
        "total": len(rows),
        "offset": offset,
        "next_offset": offset + limit if offset + limit < len(rows) else None,
        "segments": page,
        "how_to_translate": (
            "Translate each line's meaning naturally (not word for word) into the target language and keep it "
            "near target_chars so it fits the speaker's time slot; never exceed max_chars. Then call "
            "dubbing_set_translation with {id: line}."
            if lang
            else "Pass target_language to get per-line length budgets for translation."
        ),
    }


def is_same_language(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a and b and base_lang(a) == base_lang(b))
