from youtube_dubbing_mcp.subtitles import (
    detect_speakers,
    merge_cues,
    parse_any,
    parse_json3,
    parse_srt_or_vtt,
    resegment_recognition,
    to_srt,
)

from .conftest import SRT_TWO_SPEAKERS


def test_parse_srt_and_merge_and_names():
    segs = merge_cues(parse_srt_or_vtt(SRT_TWO_SPEAKERS))
    info = detect_speakers(segs)
    assert info["method"] == "names"
    assert info["speakers"] == ["Marco", "Lucia"]
    assert segs[0].text == "Hola a todos, bienvenidos."
    assert segs[1].text == "Hoy probamos el doblaje con dos voces." and segs[1].speaker == "Marco"
    assert segs[2].speaker == "Lucia" and not segs[2].text.startswith("Lucia")


def test_youtube_auto_vtt_rolling_lines_are_deduplicated():
    vtt = (
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nhello there\n\n"
        "00:00:02.000 --> 00:00:04.000\nhello there\nhow are you\n\n"
        "00:00:04.000 --> 00:00:06.000\nhow are you\nfine thanks\n"
    )
    texts = [s.text for s in parse_srt_or_vtt(vtt)]
    assert texts == ["hello there", "how are you", "fine thanks"]


def test_json3_and_turn_markers():
    data = {
        "events": [
            {"tStartMs": 0, "dDurationMs": 1500, "segs": [{"utf8": ">> So what is dubbing?"}]},
            {"tStartMs": 1600, "dDurationMs": 2000, "segs": [{"utf8": ">> It replaces the voice."}]},
            {"tStartMs": 3700, "dDurationMs": 900, "aAppend": 1, "segs": [{"utf8": "\n"}]},
        ]
    }
    segs = parse_json3(data)
    info = detect_speakers(segs)
    assert info["method"] == "turn-markers"
    assert [s.speaker for s in segs] == ["Speaker 2", "Speaker 1"] or [s.speaker for s in segs] == ["Speaker 1", "Speaker 2"]
    assert segs[0].speaker != segs[1].speaker
    assert ">>" not in segs[0].text


def test_resegment_recognition_fixes_split_words_and_sentences():
    result = {
        "text": "Hola a todos. Hoy vamos a doblar este video. Y cada persona conserva su propia voz.",
        "segments": [
            {"start": 0.0, "end": 1.2, "text": "Hola a todos"},
            {"start": 1.2, "end": 3.4, "text": "Hoy vamos a doblar este video Y cada"},
            {"start": 3.4, "end": 5.0, "text": "persona conserva su prop"},
            {"start": 5.0, "end": 5.8, "text": "ia voz"},
        ],
    }
    segs = resegment_recognition(result, silences=[(1.3, 1.6), (3.0, 3.2)])
    assert [s.text for s in segs] == [
        "Hola a todos.",
        "Hoy vamos a doblar este video.",
        "Y cada persona conserva su propia voz.",
    ]
    assert all(a.end <= b.start for a, b in zip(segs, segs[1:]))
    assert segs[1].start >= 1.3


def test_parse_any_recognition_json_and_srt_roundtrip():
    segs = parse_any('{"segments": [{"start": 0, "end": 1.5, "text": "Hi."}]}')
    assert segs[0].text == "Hi."
    srt = to_srt(segs, with_speaker=False)
    assert "00:00:00,000 --> 00:00:01,500" in srt
