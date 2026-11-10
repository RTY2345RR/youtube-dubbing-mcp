"""ffmpeg helpers: locate the binary, probe durations, extract, stretch and encode audio.

ffmpeg comes from the system PATH when installed, otherwise from the bundled
`imageio-ffmpeg` wheel, so `uvx youtube-dubbing-mcp` works with no extra setup.
ffprobe is not required: durations are read from ffmpeg's own output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import wave
from functools import lru_cache
from pathlib import Path
from typing import Optional

SAMPLE_RATE = 48_000  # YouTube's native audio rate
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
TIME_RE = re.compile(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".ts"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}


class MediaError(Exception):
    pass


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    env = os.environ.get("FFMPEG_BINARY", "").strip()
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - depends on the platform wheel
        raise MediaError(
            "ffmpeg was not found. Install it (macOS: brew install ffmpeg, Ubuntu: apt install ffmpeg, "
            "Windows: winget install ffmpeg) or set FFMPEG_BINARY."
        ) from exc


def run_ffmpeg(args: list[str], *, input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", *args]
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-6:]
        raise MediaError("ffmpeg failed: " + " | ".join(tail))
    return proc


def probe_duration(path: Path) -> float:
    """Media duration in seconds (container value, or a full decode as fallback)."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True
    )
    err = proc.stderr.decode("utf-8", "replace")
    m = DURATION_RE.search(err)
    if m:
        h, mnt, s = m.groups()
        return int(h) * 3600 + int(mnt) * 60 + float(s)
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path), "-f", "null", "-"], capture_output=True
    )
    times = TIME_RE.findall(proc.stderr.decode("utf-8", "replace"))
    if not times:
        raise MediaError(f"Could not read the duration of {path}.")
    h, mnt, s = times[-1]
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def has_video_stream(path: Path) -> bool:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True)
    return "Video:" in proc.stderr.decode("utf-8", "replace")


def extract_audio(src: Path, dest: Path, *, bitrate: str = "64k") -> Path:
    """Mono MP3 for speech recognition (small upload, enough for ASR)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", bitrate, str(dest)])
    return dest


def decode_pcm(src: Path, *, tempo: float = 1.0, rate: int = SAMPLE_RATE) -> bytes:
    """Decode to mono s16le PCM at `rate`, optionally time-stretched (pitch kept) by `tempo`."""
    args = ["-i", str(src), "-vn", "-ac", "1", "-ar", str(rate)]
    filters = []
    if abs(tempo - 1.0) > 1e-3:
        t = tempo
        while t > 2.0:  # atempo accepts 0.5..2.0 per instance
            filters.append("atempo=2.0")
            t /= 2.0
        while t < 0.5:
            filters.append("atempo=0.5")
            t /= 0.5
        filters.append(f"atempo={t:.4f}")
    # Trim leading/trailing silence so slots are used for speech, then a tiny fade.
    filters.insert(0, "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05")
    filters.extend(["areverse", "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.08", "areverse"])
    args += ["-af", ",".join(filters), "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    return run_ffmpeg(args).stdout


def pcm_seconds(pcm: bytes, rate: int = SAMPLE_RATE) -> float:
    return len(pcm) / 2 / rate


class WavWriter:
    """Stream a mono 16-bit WAV: write silence or PCM sequentially (memory stays small)."""

    def __init__(self, path: Path, rate: int = SAMPLE_RATE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.rate = rate
        self._w = wave.open(str(path), "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(rate)
        self.frames = 0

    @property
    def seconds(self) -> float:
        return self.frames / self.rate

    def silence(self, seconds: float) -> None:
        frames = int(round(seconds * self.rate))
        chunk = 48_000
        while frames > 0:
            n = min(chunk, frames)
            self._w.writeframes(b"\x00\x00" * n)
            self.frames += n
            frames -= n

    def pcm(self, data: bytes) -> None:
        if len(data) % 2:
            data = data[:-1]
        self._w.writeframes(data)
        self.frames += len(data) // 2

    def pad_to(self, seconds: float) -> None:
        missing = seconds - self.seconds
        if missing > 0:
            self.silence(missing)

    def close(self) -> None:
        self._w.close()


def encode_track(wav: Path, dest: Path, *, background: Optional[Path] = None, background_volume: float = 0.15) -> Path:
    """Encode the dubbed track. `.m4a` -> AAC 192k stereo 48 kHz, `.mp3` -> 192k, `.wav` -> PCM.

    With `background` (a music/effects stem or the original audio) it is mixed under
    the voices at `background_volume` and cut to the voice track's length.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(wav)]
    if background:
        args += ["-i", str(background), "-filter_complex",
                 f"[1:a]volume={background_volume:.3f},aresample={SAMPLE_RATE}[bg];"
                 f"[0:a][bg]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[out]",
                 "-map", "[out]"]
    ext = dest.suffix.lower()
    if ext == ".m4a":
        args += ["-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    elif ext == ".mp3":
        args += ["-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "libmp3lame", "-b:a", "192k"]
    elif ext == ".wav":
        args += ["-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le"]
    else:
        raise MediaError(f"Unsupported track format {ext}; use .m4a, .mp3 or .wav")
    run_ffmpeg([*args, str(dest)])
    return dest


def mux_preview(video: Path, audio: Path, dest: Path, srt: Optional[Path] = None) -> Path:
    """Preview MP4: original picture + dubbed audio (+ soft subtitles), no re-encode of video."""
    args = ["-i", str(video), "-i", str(audio)]
    if srt:
        args += ["-i", str(srt)]
    args += ["-map", "0:v:0", "-map", "1:a:0"]
    if srt:
        args += ["-map", "2:0", "-c:s", "mov_text"]
    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(dest)]
    run_ffmpeg(args)
    return dest


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def is_media(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS


SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([\d.]+)")


def detect_silences(path: Path, noise_db: int = -35, min_seconds: float = 0.3) -> list[tuple[float, float]]:
    """Pauses in the speech as (start, end) pairs, from ffmpeg's silencedetect filter."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path), "-af",
         f"silencedetect=n={noise_db}dB:d={min_seconds}", "-f", "null", "-"],
        capture_output=True,
    )
    err = proc.stderr.decode("utf-8", "replace")
    starts = [max(0.0, float(x)) for x in SILENCE_START_RE.findall(err)]
    ends = [float(x) for x in SILENCE_END_RE.findall(err)]
    return list(zip(starts, ends))


def median_pitch(path: Path, start: float, end: float, rate: int = 16000) -> Optional[float]:
    """Median fundamental frequency (Hz) of the voiced frames in [start, end], or None.

    A small autocorrelation pitch tracker: good enough to tell a low voice from a
    high one, which is how speakers are grouped when captions carry no names.
    """
    import array

    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-ss", f"{max(0.0, start):.3f}", "-to", f"{end:.3f}",
         "-i", str(path), "-ac", "1", "-ar", str(rate), "-f", "s16le", "-acodec", "pcm_s16le", "-"],
        capture_output=True,
    )
    samples = array.array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
    frame, hop = int(0.04 * rate), int(0.02 * rate)
    lo_lag, hi_lag = rate // 400, rate // 60
    pitches: list[float] = []
    for off in range(0, len(samples) - frame - hi_lag, hop):
        x = samples[off : off + frame + hi_lag]
        energy = sum(v * v for v in x[:frame]) / frame
        if energy < 4e5:  # quiet frame
            continue
        best_lag, best = 0, 0.0
        base = sum(v * v for v in x[:frame]) or 1
        for lag in range(lo_lag, hi_lag, 2):
            r = sum(x[i] * x[i + lag] for i in range(0, frame, 4)) * 4 / base
            if r > best:
                best, best_lag = r, lag
        if best > 0.45 and best_lag:
            pitches.append(rate / best_lag)
    if len(pitches) < 3:
        return None
    pitches.sort()
    return pitches[len(pitches) // 2]
