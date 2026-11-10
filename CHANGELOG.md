# Changelog

## 0.1.0 — 2026-11-10
- First release: 12 MCP tools for AI dubbing of YouTube URLs, local video/audio and SRT/VTT files.
- Speakers from caption markers or voice-pitch grouping; per-speaker voice samples cut from the source and cloned.
- Per-line time budgets for translation in 35+ languages; fit check for too-long lines.
- Renderer places each line at its original start, speeds up only when needed (pitch kept), and outputs a track of the exact video length, SRT, speaker SRT and preview MP4; optional background mix.
- YouTube upload package with per-language folders and Studio steps.
- Resumable jobs with saved task ids; no duplicate jobs on retry.
