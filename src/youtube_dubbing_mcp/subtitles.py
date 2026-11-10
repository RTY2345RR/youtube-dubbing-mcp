"""Caption parsing, cue merging, speaker-marker detection and SRT writing.

Supported inputs: SRT, WebVTT, YouTube json3 (what yt-dlp downloads for manual and
automatic captions) and the segment list returned by Miragea subtitle recognition.
Everything is normalised to a list of `Segment` objects with times in seconds.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

TIME_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
TAG_RE = re.compile(r"<[^>]+>")
SPEAKER_PREFIX_RE = re.compile(
    r"^\s*(?:\[(?P<b>[^\]]{1,40})\]\s*:?|\((?P<p>[^)]{1,40})\)\s*:|(?P<n>[A-Z][\w .'\-]{0,30}?)\s*:)\s+"
)
SENTENCE_END = (".", "!", "?", "…", "。", "！", "？")


@dataclass
class Segment:
    id: int
    start: float
    end: float
    text: str
    speaker: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = round(self.start, 3)
        data["end"] = round(self.end, 3)
        if not data["extra"]:
            data.pop("extra")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(
            id=int(data["id"]),
            start=float(data["start"]),
            end=float(data["end"]),
            text=str(data.get("text", "")),
            speaker=data.get("speaker"),
            extra=dict(data.get("extra") or {}),
        )


# ---------------------------------------------------------------- time utils
def parse_time(value: str) -> float:
    m = TIME_RE.search(value)
    if not m:
        raise ValueError(f"Not a timestamp: {value!r}")
    h, mnt, s, frac = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(s) + int(frac.ljust(3, "0")[:3]) / 1000


def fmt_srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def clean_text(text: str) -> str:
    text = html.unescape(TAG_RE.sub("", text))
    text = text.replace("\u200b", "").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------------- parsing
def _blocks(text: str) -> Iterable[list[str]]:
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if lines:
            yield lines


def parse_srt_or_vtt(text: str) -> list[Segment]:
    """Parse SRT or WebVTT. Rolling duplicate lines (YouTube auto VTT) are removed."""
    segments: list[Segment] = []
    previous_lines: list[str] = []
    for lines in _blocks(text.lstrip("\ufeff")):
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        left, right = lines[idx].split("-->", 1)
        try:
            start, end = parse_time(left), parse_time(right.strip().split(" ")[0])
        except ValueError:
            continue
        raw_lines = [clean_text(ln) for ln in lines[idx + 1 :]]
        raw_lines = [ln for ln in raw_lines if ln]
        # YouTube auto VTT repeats the previous line on top of the new one.
        new_lines = [ln for ln in raw_lines if ln not in previous_lines]
        if raw_lines:
            previous_lines = raw_lines
        body = " ".join(new_lines).strip()
        if not body or end <= start:
            continue
        segments.append(Segment(len(segments) + 1, start, end, body))
    return segments


def parse_json3(data: Any) -> list[Segment]:
    """Parse YouTube's json3 caption format (manual or automatic captions)."""
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    segments: list[Segment] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs or event.get("aAppend"):
            continue
        text = clean_text("".join(s.get("utf8", "") for s in segs))
        if not text:
            continue
        start = event.get("tStartMs", 0) / 1000
        end = start + event.get("dDurationMs", 0) / 1000
        if segments and start < segments[-1].end and segments[-1].text and text.startswith(segments[-1].text):
            continue
        segments.append(Segment(len(segments) + 1, start, max(end, start + 0.2), text))
    # Auto captions overlap (each cue lasts until the next-but-one); clip to the next start.
    for a, b in zip(segments, segments[1:]):
        if a.end > b.start:
            a.end = max(a.start + 0.2, b.start)
    return segments


def from_recognition(result: dict[str, Any]) -> list[Segment]:
    """Segments from a Miragea subtitle recognition result."""
    segments: list[Segment] = []
    for item in result.get("segments") or []:
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = clean_text(str(item.get("text", "")))
        if text and end > start:
            segments.append(Segment(len(segments) + 1, start, end, text))
    if not segments and result.get("srt"):
        segments = parse_srt_or_vtt(result["srt"])
    return segments


def parse_any(text: str, hint: str = "") -> list[Segment]:
    hint = hint.lower()
    stripped = text.lstrip()
    if hint.endswith("json3") or (stripped.startswith("{") and '"events"' in stripped[:2000]):
        return parse_json3(text)
    if stripped.startswith("{") or stripped.startswith("["):
        data = json.loads(text)
        if isinstance(data, list):
            data = {"segments": data}
        return from_recognition(data)
    return parse_srt_or_vtt(text)


# ------------------------------------------------------------------- merging
def merge_cues(
    segments: list[Segment],
    *,
    max_duration: float = 9.0,
    max_gap: float = 0.6,
    max_chars: int = 220,
) -> list[Segment]:
    """Merge caption fragments into sentence-sized dubbing segments.

    Caption cues are often 1-3 seconds and cut mid-sentence. Dubbing works much
    better per sentence, so cues are joined until a sentence ends, a pause is
    found, a speaker marker starts, or the limits are reached.
    """
    merged: list[Segment] = []
    for seg in segments:
        starts_new_speaker = seg.text.startswith((">>", "-", "–", "—")) or bool(SPEAKER_PREFIX_RE.match(seg.text))
        if merged:
            cur = merged[-1]
            joinable = (
                not starts_new_speaker
                and (seg.speaker is None or seg.speaker == cur.speaker)
                and seg.start - cur.end <= max_gap
                and seg.end - cur.start <= max_duration
                and len(cur.text) + len(seg.text) + 1 <= max_chars
                and not cur.text.rstrip().endswith(SENTENCE_END)
            )
            if joinable:
                cur.text = f"{cur.text} {seg.text}".strip()
                cur.end = max(cur.end, seg.end)
                continue
        merged.append(Segment(len(merged) + 1, seg.start, seg.end, seg.text, seg.speaker, dict(seg.extra)))
    return merged


# ---------------------------------------------------------- speaker markers
NOT_SPEAKERS = {
    "note", "notes", "today", "update", "warning", "step", "tip", "tips", "example", "chapter", "part", "edit",
    "source", "music", "translation", "subtitles", "captions", "summary", "question", "answer", "q", "a", "pro tip",
}


def _prefix_name(text: str) -> Optional[str]:
    m = SPEAKER_PREFIX_RE.match(text)
    if not m:
        return None
    name = (m.group("b") or m.group("p") or m.group("n") or "").strip()
    if not name or len(name.split()) > 4 or name.lower() in NOT_SPEAKERS or name.lower().startswith(("http", "step ")):
        return None
    if m.group("b") and name.lower() in ("music", "applause", "laughter", "laughs", "inaudible"):
        return None
    return name


def detect_speakers(segments: list[Segment], default: str = "Speaker 1") -> dict[str, Any]:
    """Infer speakers from caption conventions and strip the markers from the text.

    Recognised: "Name: text", "[Name] text", "(Name): text", and YouTube's ">>"
    / leading dash for a change of speaker (alternates Speaker 1 / Speaker 2).
    Returns {"speakers": [...], "method": ..., "changes": n}. Segments are edited in place.
    """
    counts: dict[str, int] = {}
    toggles = 0
    for seg in segments:
        name = _prefix_name(seg.text)
        if name:
            counts[name] = counts.get(name, 0) + 1
        elif seg.text.startswith((">>", "- ", "– ", "— ")):
            toggles += 1
    # One stray "Today: ..." is not a speaker; a dialogue has 2+ names or a repeated one.
    valid = set(counts) if (len(counts) >= 2 or any(n >= 2 for n in counts.values())) else set()
    named = sum(counts[n] for n in valid)

    current = default
    others = ["Speaker 1", "Speaker 2"]
    method = "none"
    if named:
        method = "names"
    elif toggles:
        method = "turn-markers"
    for seg in segments:
        if method == "names":
            m = SPEAKER_PREFIX_RE.match(seg.text)
            name = _prefix_name(seg.text)
            if m and name in valid:
                current = name
                seg.text = seg.text[m.end() :].strip()
        elif method == "turn-markers" and seg.text.startswith((">>", "- ", "– ", "— ")):
            current = others[1] if current == others[0] else others[0]
            seg.text = seg.text.lstrip(">-–— ").strip()
        seg.text = seg.text.replace(">>", "").strip()
        if seg.speaker is None or method != "none":
            seg.speaker = current
    speakers: list[str] = []
    for seg in segments:
        if seg.speaker and seg.speaker not in speakers:
            speakers.append(seg.speaker)
    changes = sum(1 for a, b in zip(segments, segments[1:]) if a.speaker != b.speaker)
    return {"speakers": speakers, "method": method, "changes": changes}


# ------------------------------------------------------------------- writing
def to_srt(segments: Iterable[Segment], *, with_speaker: bool = False) -> str:
    out = []
    for n, seg in enumerate(segments, 1):
        text = seg.text
        if with_speaker and seg.speaker:
            text = f"[{seg.speaker}] {text}"
        out.append(f"{n}\n{fmt_srt_time(seg.start)} --> {fmt_srt_time(seg.end)}\n{text}\n")
    return "\n".join(out)


def renumber(segments: list[Segment]) -> list[Segment]:
    for n, seg in enumerate(segments, 1):
        seg.id = n
    return segments





# ------------------------------------------------- recognition resegmenting
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…。！？])\s+|(?<=[。！？])")


def _norm_chars(text: str) -> list[tuple[int, str]]:
    return [(i, ch.lower()) for i, ch in enumerate(text) if ch.isalnum()]


def resegment_recognition(
    result: dict[str, Any],
    silences: Optional[list[tuple[float, float]]] = None,
    max_duration: float = 12.0,
) -> list[Segment]:
    """Sentence segments from a recognition result.

    Recognisers return caption-sized chunks that may cut sentences (or even
    words) in half, plus the full punctuated text. Each character of the full
    text is timed by interpolating inside the chunk it falls in, the text is
    split into sentences, and sentence edges are snapped to detected pauses.
    """
    chunks = from_recognition(result)
    full = clean_text(str(result.get("text") or result.get("txt") or ""))
    if not chunks:
        return []
    if not full:
        return chunks
    # character timeline from chunks
    times: list[float] = []
    stream: list[str] = []
    for seg in chunks:
        chars = [ch.lower() for ch in seg.text if ch.isalnum()]
        n = max(1, len(chars))
        for k, ch in enumerate(chars):
            stream.append(ch)
            times.append(seg.start + (seg.end - seg.start) * (k + 0.5) / n)
    full_chars = _norm_chars(full)
    if not stream or not full_chars:
        return chunks
    import difflib

    matcher = difflib.SequenceMatcher(None, [c for _, c in full_chars], stream, autojunk=False)
    char_time: dict[int, float] = {}
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            char_time[full_chars[a + k][0]] = times[b + k]
    if len(char_time) < 0.6 * len(full_chars):
        return chunks

    sentences: list[tuple[int, int]] = []
    pos = 0
    for part in _SENT_SPLIT_RE.split(full):
        if not part.strip():
            pos += len(part)
            continue
        start = full.index(part, pos)
        sentences.append((start, start + len(part)))
        pos = start + len(part)

    def first_time(a: int, b: int) -> Optional[float]:
        for i in range(a, b):
            if i in char_time:
                return char_time[i]
        return None

    def last_time(a: int, b: int) -> Optional[float]:
        for i in range(b - 1, a - 1, -1):
            if i in char_time:
                return char_time[i]
        return None

    out: list[Segment] = []
    for a, b in sentences:
        t0, t1 = first_time(a, b), last_time(a, b)
        if t0 is None or t1 is None:
            if out:
                out[-1].text += " " + full[a:b].strip()
            continue
        out.append(Segment(len(out) + 1, t0, max(t1, t0 + 0.3), full[a:b].strip()))
    if not out:
        return chunks
    # Character centres sit inside the speech; snap edges to pauses (or pad slightly).
    for i, seg in enumerate(out):
        lo = out[i - 1].end if i else 0.0
        hi = out[i + 1].start if i + 1 < len(out) else seg.end + 1.0
        start, end = max(lo, seg.start - 0.25), min(hi, seg.end + 0.25)
        for s_start, s_end in silences or []:
            if seg.start - 1.0 <= s_end <= seg.start + 0.5 and s_end >= lo:
                start = s_end
            if seg.end - 0.5 <= s_start <= seg.end + 1.0 and s_start <= hi:
                end = min(end, s_start) if s_start > seg.start else end
        seg.start, seg.end = round(max(lo, start), 3), round(max(start + 0.3, end), 3)
    # split very long sentences at commas
    final: list[Segment] = []
    for seg in out:
        if seg.duration <= max_duration or "," not in seg.text:
            final.append(seg)
            continue
        parts = [p.strip() for p in re.split(r"(?<=,)\s+", seg.text) if p.strip()]
        total = sum(len(p) for p in parts) or 1
        t = seg.start
        for p in parts:
            d = seg.duration * len(p) / total
            final.append(Segment(0, t, t + d, p))
            t += d
    return renumber(final)


__all__ = [
    "Segment",
    "clean_text",
    "detect_speakers",
    "fmt_srt_time",
    "from_recognition",
    "merge_cues",
    "parse_any",
    "parse_json3",
    "parse_srt_or_vtt",
    "parse_time",
    "renumber",
    "resegment_recognition",
    "to_srt",
]
