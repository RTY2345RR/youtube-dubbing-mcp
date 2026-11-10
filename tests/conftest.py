"""Fake voice engine on httpx.MockTransport: no network, no API key, real ffmpeg."""

from __future__ import annotations

import io
import json
import math
import struct
import wave
from typing import Any

import httpx
import pytest

from youtube_dubbing_mcp import server

API_KEY = "sk-test-SECRET-0000"


def tone_wav(seconds: float, freq: float = 220.0, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * n / rate))) for n in range(int(seconds * rate))
        )
        w.writeframes(frames)
    return buf.getvalue()


CAPS = {
    "api_version": "1",
    "audio": {
        "models": [{"id": "expressive", "uses_reference_text": True}, {"id": "standard", "uses_reference_text": False}],
        "default_model": "expressive",
        "voice_sources": ["actor_id", "reference_audio_url"],
        "max_text_length": 5000,
    },
    "subtitles": {"points_per_second": 3, "max_upload_mb": 100, "max_duration_minutes": 90},
}

RECOGNITION = {
    "language": "es",
    "segments": [
        {"start": 0.0, "end": 2.4, "text": "Hola a todos."},
        {"start": 2.6, "end": 5.0, "text": "Hoy doblamos este video."},
        {"start": 5.4, "end": 8.0, "text": "Y cada voz suena como la original."},
    ],
}


class FakeEngine:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.keys: list[str] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.clip_seconds = 1.2
        self.fail_text: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=tone_wav(self.clip_seconds))
        assert request.headers.get("authorization") == f"Bearer {API_KEY}"
        if path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPS)
        if path.endswith("/actors"):
            return httpx.Response(200, json={"actors": [{"actor_id": "42", "name": "Narrator", "language": "en"}], "has_more": False})
        if path.endswith("/audio/generations"):
            body = json.loads(request.content)
            key = request.headers["idempotency-key"]
            self.created.append(body)
            self.keys.append(key)
            tid = f"t{len(self.created)}"
            failed = self.fail_text and self.fail_text in body["text"]
            self.tasks[tid] = {
                "task_id": tid,
                "status": "failed" if failed else "succeeded",
                "result": None if failed else {"audio_url": f"https://cdn.test/{tid}.wav"},
                "error": {"code": "synthesis_failed", "message": "boom", "retryable": True} if failed else None,
            }
            return httpx.Response(202, json={"task_id": tid, "status": "queued"})
        if path.endswith("/subtitles/recognitions"):
            self.tasks["sub1"] = {"task_id": "sub1", "status": "succeeded", "result": RECOGNITION}
            return httpx.Response(202, json={"task_id": "sub1", "status": "queued"})
        if "/tasks/" in path:
            return httpx.Response(200, json=self.tasks[path.rsplit("/", 1)[1]])
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})


@pytest.fixture
def engine(monkeypatch, tmp_path):
    fake = FakeEngine()
    monkeypatch.setenv("MIRAGEA_API_KEY", API_KEY)
    monkeypatch.setenv("DUBBING_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(server, "_TRANSPORT", httpx.MockTransport(fake.handler))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(server, "_SLEEP", no_sleep)
    return fake


@pytest.fixture
def sample_host(monkeypatch):
    uploads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        uploads.append(request.url.host)
        return httpx.Response(200, json={"success": True, "files": [{"url": f"https://d.host.test/{len(uploads)}.wav"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "_SAMPLE_HTTP", client)
    return uploads


SRT_TWO_SPEAKERS = """1
00:00:00,000 --> 00:00:02,500
Marco: Hola a todos, bienvenidos.

2
00:00:02,600 --> 00:00:04,000
Hoy probamos el doblaje

3
00:00:04,000 --> 00:00:05,500
con dos voces.

4
00:00:06,000 --> 00:00:09,000
Lucia: Y yo conservo mi propia voz.
"""


@pytest.fixture
def srt_file(tmp_path):
    p = tmp_path / "talk.es.srt"
    p.write_text(SRT_TWO_SPEAKERS, encoding="utf-8")
    return p


def call(coro):
    import anyio

    return anyio.run(lambda: coro)
