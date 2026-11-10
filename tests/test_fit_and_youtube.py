from youtube_dubbing_mcp.fit import budgets, chars_per_second, check_translation, tempo_for
from youtube_dubbing_mcp.project import ProjectError, normalize_language
from youtube_dubbing_mcp.subtitles import Segment
from youtube_dubbing_mcp.youtube import is_youtube_url, pick_caption_track

import pytest


def test_budgets_scale_with_language_and_slot():
    segs = [Segment(1, 0, 2, "a"), Segment(2, 3, 5, "b")]
    en = budgets(segs, "en", 6.0)
    ja = budgets(segs, "ja", 6.0)
    assert en[0]["slot_seconds"] == pytest.approx(2.95, abs=0.01)
    assert en[0]["target_chars"] > ja[0]["target_chars"]
    assert chars_per_second("pt-BR") == chars_per_second("pt")


def test_check_translation_flags_long_and_missing():
    segs = [Segment(1, 0, 1, "x"), Segment(2, 1, 2, "y")]
    report = check_translation(segs, {1: "This line is far too long to be spoken in under one second."}, "en", 2.0)
    assert report["missing"] == [2]
    assert report["too_long"][0]["id"] == 1
    assert not report["fits"]


def test_tempo_caps_speedup():
    assert tempo_for(2.0, 2.5, 1.25) == (1.0, 0.0)
    tempo, overflow = tempo_for(3.0, 2.0, 1.25)
    assert tempo == 1.25 and overflow == pytest.approx(0.4)


def test_language_codes():
    assert normalize_language("pt_br") == "pt-BR"
    assert normalize_language("ES") == "es"
    with pytest.raises(ProjectError):
        normalize_language("spanish please")


def test_youtube_url_and_caption_choice():
    assert is_youtube_url("https://youtu.be/abc") and is_youtube_url("https://www.youtube.com/watch?v=x")
    assert not is_youtube_url("https://vimeo.com/1")
    info = {
        "language": "en",
        "subtitles": {},
        "automatic_captions": {
            "es": [{"ext": "json3", "url": "https://x/es"}],
            "en-orig": [{"ext": "vtt", "url": "https://x/en-orig.vtt"}, {"ext": "json3", "url": "https://x/en-orig"}],
            "en": [{"ext": "json3", "url": "https://x/en"}],
        },
    }
    track = pick_caption_track(info)
    assert track == {"url": "https://x/en-orig", "ext": "json3", "language": "en", "kind": "automatic"}
    manual = dict(info, subtitles={"en": [{"ext": "vtt", "url": "https://x/manual.vtt"}]})
    assert pick_caption_track(manual)["kind"] == "manual"


def test_bot_check_error_gets_cookie_hint(monkeypatch):
    from youtube_dubbing_mcp import youtube

    class Boom:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, *a, **k):
            raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setenv("YTDLP_COOKIES_FROM_BROWSER", "firefox")
    seen = {}

    def fake(opts):
        seen.update(opts)
        return Boom(opts)

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", lambda opts: fake(opts))
    with pytest.raises(youtube.YouTubeError, match="YTDLP_COOKIES_FROM_BROWSER"):
        youtube.fetch_info("https://youtu.be/x")
    assert seen["cookiesfrombrowser"] == ("firefox",)
