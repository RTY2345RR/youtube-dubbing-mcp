import json
import math
import struct
import wave
from pathlib import Path

import anyio
import pytest

from youtube_dubbing_mcp import server
from youtube_dubbing_mcp.media import probe_duration

try:
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # SDK 1.x
    from mcp.server.fastmcp.exceptions import ToolError


def run(fn, *args, **kwargs):
    return anyio.run(lambda: fn(*args, **kwargs))


EN = {"1": "Hi everyone, welcome.", "2": "Today we test dubbing with two voices.", "3": "And I keep my own voice."}


def _srt_project(srt_file):
    p = run(server.dubbing_start_project, source=str(srt_file), source_language="es")
    assert p["segments"] == 3 and [s["name"] for s in p["speakers"]] == ["Marco", "Lucia"]
    return p["project_id"]


def _voices(pid):
    run(server.dubbing_set_voice, pid, "Marco", reference_audio_url="https://ex.test/m.wav", reference_text="ref m")
    run(server.dubbing_set_voice, pid, "Lucia", actor_id="42")


def test_full_flow_from_srt(engine, srt_file):
    pid = _srt_project(srt_file)
    seg = run(server.dubbing_get_segments, pid, target_language="en")
    assert seg["segments"][0]["target_chars"] > 10 and seg["next_offset"] is None
    with pytest.raises(ToolError, match="No voice"):
        run(server.dubbing_set_translation, pid, "en", EN)
        run(server.dubbing_render, pid, "en")
    _voices(pid)
    fit = run(server.dubbing_set_translation, pid, "en", EN, title="My video (English)")
    assert fit["fits"] and fit["title"] == "My video (English)"
    out = run(server.dubbing_render, pid, "en", audio_format="mp3")
    assert out["lines"] == 3 and out["sped_up_lines"] == 0
    assert abs(probe_duration(Path(out["track"])) - out["video_seconds"]) < 0.15
    srt = Path(out["captions"]).read_text()
    assert "Hi everyone, welcome." in srt and "[Marco]" in Path(out["captions_with_speakers"]).read_text()
    bodies = engine.created
    assert bodies[0]["reference_audio_url"] == "https://ex.test/m.wav" and bodies[0]["reference_text"] == "ref m"
    assert any(b.get("actor_id") == "42" for b in bodies)
    pkg = run(server.dubbing_export_youtube_package, pid)
    folder = Path(pkg["languages"][0]["folder"])
    assert (folder / "audio.mp3").exists() and (folder / "captions.srt").exists()
    assert (folder / "title.txt").read_text().strip() == "My video (English)"
    assert "YouTube Studio" in Path(pkg["instructions"]).read_text()
    status = run(server.dubbing_project_status, pid)
    assert status["languages"]["en"]["rendered"]
    listed = run(server.dubbing_list_projects)
    assert listed["projects"][0]["project_id"] == pid


def test_rerender_reuses_lines_and_only_redoes_changed(engine, srt_file):
    pid = _srt_project(srt_file)
    _voices(pid)
    run(server.dubbing_set_translation, pid, "en", EN)
    run(server.dubbing_render, pid, "en")
    assert len(engine.created) == 3
    run(server.dubbing_render, pid, "en")
    assert len(engine.created) == 3  # nothing resubmitted
    run(server.dubbing_set_translation, pid, "en", {"2": "Today: dubbing, two voices."})
    run(server.dubbing_render, pid, "en")
    assert len(engine.created) == 4 and engine.created[-1]["text"] == "Today: dubbing, two voices."


def test_long_lines_are_sped_up_within_cap(engine, srt_file):
    pid = _srt_project(srt_file)
    _voices(pid)
    run(server.dubbing_set_translation, pid, "en", EN)
    engine.clip_seconds = 4.0  # longer than segment 1's slot
    out = run(server.dubbing_render, pid, "en", max_speedup=1.25)
    assert out["max_tempo"] == pytest.approx(1.25, abs=0.01)
    assert out["overflowing_lines"]


def test_failed_line_keeps_finished_ones(engine, srt_file):
    pid = _srt_project(srt_file)
    _voices(pid)
    run(server.dubbing_set_translation, pid, "en", EN)
    engine.fail_text = "two voices"
    with pytest.raises(ToolError, match="1 of 3 lines failed"):
        run(server.dubbing_render, pid, "en")
    engine.fail_text = None
    run(server.dubbing_render, pid, "en")
    assert len(engine.created) == 4  # only the failed line was submitted again


def test_assign_speakers_and_validation(engine, srt_file):
    pid = _srt_project(srt_file)
    out = run(server.dubbing_assign_speakers, pid, {"*": "Host", "3": "Guest"})
    assert out["speakers"] == ["Host", "Guest"]
    with pytest.raises(ToolError, match="does not exist"):
        run(server.dubbing_assign_speakers, pid, {"9": "X"})
    with pytest.raises(ToolError, match="exactly one"):
        run(server.dubbing_set_voice, pid, "Host")
    with pytest.raises(ToolError, match="public http"):
        run(server.dubbing_set_voice, pid, "Host", reference_audio_url="/tmp/me.wav")
    with pytest.raises(ToolError, match="Unknown speaker"):
        run(server.dubbing_set_voice, pid, "Nobody", actor_id="1")
    with pytest.raises(ToolError, match="Invalid project_id"):
        run(server.dubbing_project_status, "../etc")


def _two_voice_wav(path: Path, rate: int = 16000) -> None:
    def tone(freq, secs):
        return b"".join(
            struct.pack("<h", int(9000 * (math.sin(2 * math.pi * freq * n / rate) + 0.4 * math.sin(4 * math.pi * freq * n / rate))))
            for n in range(int(secs * rate))
        )

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(tone(110, 2.4) + b"\x00\x00" * int(0.2 * rate) + tone(115, 2.4) + b"\x00\x00" * int(0.4 * rate) + tone(210, 2.6))


def test_media_source_is_transcribed_grouped_by_voice_and_cloned(engine, sample_host, tmp_path):
    audio = tmp_path / "talk.wav"
    _two_voice_wav(audio)
    p = run(server.dubbing_start_project, source=str(audio))
    assert p["transcript_origin"] == "speech-recognition" and p["source_language"] == "es"
    assert p["speaker_detection"]["method"] == "voice-pitch"
    assert [s["name"] for s in p["speakers"]] == ["Speaker 1", "Speaker 2"]
    seg = run(server.dubbing_get_segments, p["project_id"])
    assert [s["speaker"] for s in seg["segments"]] == ["Speaker 1", "Speaker 1", "Speaker 2"]
    out = run(server.dubbing_extract_voice_samples, p["project_id"], share=True)
    assert out["missing_voices"] == [] and len(sample_host) == 2
    assert out["samples"][0]["reference_audio_url"].startswith("https://d.host.test/")
    assert Path(out["samples"][0]["sample"]).exists()
    run(server.dubbing_set_translation, p["project_id"], "en", {"1": "Hi all.", "2": "We dub this video.", "3": "Each voice sounds right."})
    r = run(server.dubbing_render, p["project_id"], "en")
    assert r["track_seconds"] == pytest.approx(r["video_seconds"], abs=0.15)
    assert engine.created[0]["reference_audio_url"].startswith("https://d.host.test/")


def test_check_setup_hides_key(engine):
    out = run(server.dubbing_check_setup)
    assert out["ready"] and out["api_key"] == "set"
    assert "SECRET" not in json.dumps(out)
    assert "pricing" not in json.dumps(out)


def test_missing_key_message(monkeypatch, tmp_path, srt_file):
    monkeypatch.delenv("MIRAGEA_API_KEY", raising=False)
    monkeypatch.setenv("DUBBING_OUTPUT_DIR", str(tmp_path))
    out = run(server.dubbing_check_setup)
    assert out["api_key"] == "missing" and "miragea.ai/workspace/api" in out["next_step"]
    p = run(server.dubbing_start_project, source=str(srt_file))  # captions need no key
    with pytest.raises(ToolError, match="MIRAGEA_API_KEY"):
        run(server.dubbing_render, p["project_id"], "en")


def test_youtube_url_uses_captions_and_exports_studio_link(engine, monkeypatch, tmp_path):
    from youtube_dubbing_mcp import youtube

    audio = tmp_path / "a.wav"
    _two_voice_wav(audio)
    info = {
        "id": "abc123", "title": "How dubbing works", "duration": 8.0, "language": "en",
        "automatic_captions": {"en-orig": [{"ext": "json3", "url": "https://caps.test/en"}]},
    }
    caps = {"events": [
        {"tStartMs": 0, "dDurationMs": 2400, "segs": [{"utf8": ">> Welcome back to the channel."}]},
        {"tStartMs": 2600, "dDurationMs": 2400, "segs": [{"utf8": "Today: dubbing."}]},
        {"tStartMs": 5400, "dDurationMs": 2600, "segs": [{"utf8": ">> Thanks for having me."}]},
    ]}

    def fake_audio(url, dest):
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / "source-audio.wav"
        out.write_bytes(audio.read_bytes())
        return out

    monkeypatch.setattr(youtube, "fetch_info", lambda url: info)
    monkeypatch.setattr(youtube, "download_audio", fake_audio)
    monkeypatch.setattr(youtube, "download_text", lambda url: json.dumps(caps))
    p = run(server.dubbing_start_project, source="https://www.youtube.com/watch?v=abc123")
    assert p["transcript_origin"] == "youtube-automatic-captions" and p["title"] == "How dubbing works"
    assert p["speaker_detection"]["method"] == "turn-markers" and len(p["speakers"]) == 2
    pid = p["project_id"]
    for name in [s["name"] for s in p["speakers"]]:
        run(server.dubbing_set_voice, pid, name, actor_id="42")
    run(server.dubbing_set_translation, pid, "es", {"1": "Bienvenidos al canal.", "2": "Hoy: doblaje.", "3": "Gracias por invitarme."})
    run(server.dubbing_render, pid, "es")
    pkg = run(server.dubbing_export_youtube_package, pid, ["es"])
    assert pkg["studio_url"] == "https://studio.youtube.com/video/abc123/translations"
    assert json.loads((Path(pkg["folder"]) / "manifest.json").read_text())["source_language"] == "en"
