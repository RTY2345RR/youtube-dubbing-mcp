"""Length-fit: how much translated text fits a time slot, and checking translations against it.

Dubbed speech must start with the original line and end before the next one. Each
language is spoken at a different number of characters per second, so a budget
is computed per segment from the slot length and a typical speaking rate. The
host agent translates with these budgets and shortens any line that is over.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

from .subtitles import Segment

# Typical conversational speaking rates in characters per second (spaces and
# punctuation included). CJK counts characters, Japanese counts kana/kanji.
CHARS_PER_SECOND: dict[str, float] = {
    "en": 15.0, "es": 16.0, "pt": 15.5, "fr": 15.5, "it": 15.5, "de": 14.5, "nl": 15.0,
    "sv": 14.5, "no": 14.5, "da": 14.5, "fi": 13.5, "pl": 14.0, "cs": 14.0, "ro": 15.0,
    "ru": 14.0, "uk": 14.0, "tr": 14.5, "el": 14.5, "hu": 14.0, "id": 15.5, "ms": 15.5,
    "vi": 14.0, "tl": 15.0, "hi": 13.5, "bn": 13.0, "ta": 12.5, "ur": 13.5, "ar": 13.0,
    "he": 13.5, "fa": 13.5, "th": 12.0, "zh": 5.5, "yue": 5.5, "ja": 8.0, "ko": 7.0,
}
DEFAULT_CPS = 14.5
MIN_SLOT = 0.4


def base_lang(code: str) -> str:
    return (code or "en").lower().replace("_", "-").split("-")[0]


def chars_per_second(language: str) -> float:
    lang = (language or "en").lower()
    if lang in ("zh-hk", "zh-yue"):
        return CHARS_PER_SECOND["yue"]
    return CHARS_PER_SECOND.get(base_lang(lang), DEFAULT_CPS)


def spoken_length(text: str) -> int:
    """Characters that take speaking time (markup like [laugh] and extra spaces removed)."""
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return sum(1 for ch in text if not unicodedata.category(ch).startswith("C"))


def slot_seconds(segments: list[Segment], index: int, duration: Optional[float], tail: float = 0.6) -> float:
    """Time available to segment `index`: until the next segment starts (or the video ends)."""
    seg = segments[index]
    if index + 1 < len(segments):
        limit = segments[index + 1].start
    else:
        limit = seg.end + tail if duration is None else min(duration, seg.end + max(tail, 1.5))
    return max(MIN_SLOT, limit - seg.start - 0.05)


def budgets(
    segments: list[Segment], language: str, duration: Optional[float], max_speedup: float = 1.25
) -> list[dict[str, Any]]:
    cps = chars_per_second(language)
    rows = []
    for i, seg in enumerate(segments):
        slot = slot_seconds(segments, i, duration)
        rows.append(
            {
                "id": seg.id,
                "speaker": seg.speaker,
                "start": round(seg.start, 2),
                "slot_seconds": round(slot, 2),
                "target_chars": max(4, int(slot * cps)),
                "max_chars": max(6, int(slot * cps * max_speedup)),
                "text": seg.text,
            }
        )
    return rows


def check_translation(
    segments: list[Segment],
    translations: dict[int, str],
    language: str,
    duration: Optional[float],
    max_speedup: float = 1.25,
) -> dict[str, Any]:
    """Compare translated lines to their budgets. Returns too_long / missing / ok counts."""
    rows = {r["id"]: r for r in budgets(segments, language, duration, max_speedup)}
    too_long, missing = [], []
    for seg in segments:
        text = (translations.get(seg.id) or "").strip()
        if not text:
            missing.append(seg.id)
            continue
        n = spoken_length(text)
        row = rows[seg.id]
        if n > row["max_chars"]:
            too_long.append(
                {
                    "id": seg.id,
                    "chars": n,
                    "max_chars": row["max_chars"],
                    "target_chars": row["target_chars"],
                    "over_by_percent": round(100 * (n - row["max_chars"]) / row["max_chars"]),
                    "text": text,
                }
            )
    return {
        "language": language,
        "segments": len(segments),
        "missing": missing,
        "too_long": too_long,
        "fits": not too_long and not missing,
        "advice": (
            "All lines fit their time slots."
            if not too_long and not missing
            else "Shorten the too_long lines to about target_chars (keep the meaning, drop filler), "
            "fill missing ids, then call set_translation again with only those ids."
        ),
    }


def tempo_for(clip_seconds: float, slot: float, max_speedup: float) -> tuple[float, float]:
    """(tempo to apply, seconds that still overflow the slot after speeding up)."""
    if clip_seconds <= slot or slot <= 0:
        return 1.0, 0.0
    needed = clip_seconds / slot
    tempo = min(needed, max_speedup)
    overflow = clip_seconds / tempo - slot
    return round(tempo, 4), round(max(0.0, overflow), 3)
