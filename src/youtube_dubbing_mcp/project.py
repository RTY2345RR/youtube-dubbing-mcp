"""A dubbing project on disk: one folder with project.json, the source media and every output.

Everything a later step needs is saved, so any tool can be re-run after a crash,
a client restart or a timeout and continue where it stopped.

    <output_dir>/projects/<project_id>/
        project.json              state (segments, speakers, voices, translations, jobs)
        source/                   downloaded or copied audio/video, original captions
        samples/<speaker>.wav     voice samples cut from the source (optional)
        lines/<lang>/0001.wav     one synthesized clip per segment
        dub/<lang>/...            dubbed track, captions, preview video
        youtube-upload/           the package for YouTube Studio
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .subtitles import Segment

PROJECT_FILE = "project.json"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class ProjectError(Exception):
    pass


def slugify(text: str, limit: int = 40) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text or "")[:8]
    slug = "-".join(words).lower()[:limit].strip("-")
    return slug or "dub"


def normalize_language(code: str) -> str:
    code = (code or "").strip().replace("_", "-")
    if not LANG_RE.match(code):
        raise ProjectError(f"{code!r} is not a language code. Use a BCP-47 code such as en, es, pt-BR, de or ja.")
    parts = code.split("-")
    return "-".join([parts[0].lower()] + [p.upper() if len(p) == 2 else p for p in parts[1:]])


def speaker_slug(name: str) -> str:
    return slugify(name, 30) if re.search(r"[A-Za-z0-9]", name or "") else hashlib.sha1(name.encode()).hexdigest()[:8]


def text_hash(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class Project:
    root: Path
    data: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- creation
    @classmethod
    def create(cls, projects_dir: Path, name: str, source: str, source_kind: str) -> "Project":
        projects_dir.mkdir(parents=True, exist_ok=True)
        base = slugify(name)
        stamp = time.strftime("%Y%m%d")
        project_id = f"{base}-{stamp}"
        n = 2
        while (projects_dir / project_id).exists():
            project_id = f"{base}-{stamp}-{n}"
            n += 1
        root = projects_dir / project_id
        (root / "source").mkdir(parents=True)
        data = {
            "id": project_id,
            "version": 1,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": source,
            "source_kind": source_kind,
            "title": name,
            "description": "",
            "duration": None,
            "source_language": None,
            "transcript_origin": None,
            "media": {},
            "segments": [],
            "speakers": {},
            "translations": {},
            "metadata": {},
            "jobs": {},
            "renders": {},
        }
        project = cls(root, data)
        project.save()
        return project

    @classmethod
    def load(cls, projects_dir: Path, project_id: str) -> "Project":
        project_id = (project_id or "").strip()
        if not ID_RE.match(project_id):
            raise ProjectError(f"Invalid project_id {project_id!r}. Call dubbing_list_projects to see yours.")
        root = projects_dir / project_id
        path = root / PROJECT_FILE
        if not path.exists():
            raise ProjectError(f"No dubbing project {project_id!r} in {projects_dir}. Call dubbing_list_projects.")
        return cls(root, json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def list(projects_dir: Path) -> list[dict[str, Any]]:
        rows = []
        if not projects_dir.exists():
            return rows
        for path in sorted(projects_dir.glob(f"*/{PROJECT_FILE}"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append(
                {
                    "project_id": data.get("id"),
                    "title": data.get("title"),
                    "source": data.get("source"),
                    "segments": len(data.get("segments") or []),
                    "speakers": list((data.get("speakers") or {}).keys()),
                    "languages": sorted((data.get("translations") or {}).keys()),
                    "rendered": sorted((data.get("renders") or {}).keys()),
                }
            )
        return rows

    # ----------------------------------------------------------- persistence
    @property
    def id(self) -> str:
        return self.data["id"]

    def save(self) -> None:
        path = self.root / PROJECT_FILE
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def media_path(self, kind: str) -> Optional[Path]:
        value = (self.data.get("media") or {}).get(kind)
        if not value:
            return None
        p = Path(value)
        p = p if p.is_absolute() else self.root / p
        return p if p.exists() else None

    def set_media(self, kind: str, path: Path) -> None:
        try:
            rel = path.resolve().relative_to(self.root.resolve())
            self.data.setdefault("media", {})[kind] = rel.as_posix()
        except ValueError:
            self.data.setdefault("media", {})[kind] = str(path.resolve())

    # -------------------------------------------------------------- segments
    @property
    def segments(self) -> list[Segment]:
        return [Segment.from_dict(s) for s in self.data.get("segments") or []]

    def set_segments(self, segments: list[Segment]) -> None:
        self.data["segments"] = [s.to_dict() for s in segments]
        for seg in segments:
            if seg.speaker and seg.speaker not in self.data["speakers"]:
                self.data["speakers"][seg.speaker] = {}

    def speaker_names(self) -> list[str]:
        names: list[str] = []
        for seg in self.segments:
            if seg.speaker and seg.speaker not in names:
                names.append(seg.speaker)
        return names

    def duration(self) -> Optional[float]:
        value = self.data.get("duration")
        if value:
            return float(value)
        segs = self.segments
        return segs[-1].end + 1.0 if segs else None

    # ------------------------------------------------------------ voices
    def voice(self, speaker: str) -> dict[str, Any]:
        return dict((self.data.get("speakers") or {}).get(speaker) or {})

    def missing_voices(self) -> list[str]:
        return [s for s in self.speaker_names() if not (self.voice(s).get("actor_id") or self.voice(s).get("reference_audio_url"))]

    # -------------------------------------------------------- translations
    def translation(self, language: str) -> dict[int, str]:
        raw = (self.data.get("translations") or {}).get(language) or {}
        return {int(k): v for k, v in raw.items()}

    def summary(self) -> dict[str, Any]:
        segs = self.segments
        speakers = self.speaker_names()
        per_speaker = {s: sum(1 for x in segs if x.speaker == s) for s in speakers}
        langs = {}
        for lang in sorted((self.data.get("translations") or {}).keys()):
            tr = self.translation(lang)
            render = (self.data.get("renders") or {}).get(lang) or {}
            langs[lang] = {
                "translated": sum(1 for s in segs if (tr.get(s.id) or "").strip()),
                "of": len(segs),
                "rendered": bool(render.get("track")),
                "track": render.get("track"),
            }
        return {
            "project_id": self.id,
            "title": self.data.get("title"),
            "source": self.data.get("source"),
            "source_language": self.data.get("source_language"),
            "transcript_origin": self.data.get("transcript_origin"),
            "duration_seconds": round(self.duration() or 0, 2),
            "segments": len(segs),
            "speakers": [
                {
                    "name": s,
                    "segments": per_speaker[s],
                    "voice": _public_voice(self.voice(s)),
                }
                for s in speakers
            ],
            "missing_voices": self.missing_voices(),
            "languages": langs,
            "folder": str(self.root),
        }


def _public_voice(voice: dict[str, Any]) -> Optional[dict[str, Any]]:
    if voice.get("actor_id"):
        return {"actor_id": voice["actor_id"], "model": voice.get("model")}
    if voice.get("reference_audio_url"):
        return {
            "reference_audio_url": voice["reference_audio_url"],
            "has_reference_text": bool(voice.get("reference_text")),
            "model": voice.get("model"),
        }
    return None
