# Example: dub a two-speaker video from Spanish to English

Real output of `youtube-dubbing-mcp` (no edits). Two speakers, one male (Marco) and one female (Lucia), each dubbed into English in a voice cloned from their own Spanish lines.

| File | What it is |
| --- | --- |
| [`source-interview-es.mp4`](source-interview-es.mp4) | the original Spanish clip (11.3 s) |
| [`transcript.es.speakers.srt`](transcript.es.speakers.srt) | transcript after sentence splitting and speaker detection |
| [`dub.en.mp3`](dub.en.mp3) | the English dub track, same length as the video |
| [`dub.en.srt`](dub.en.srt) / [`dub.en.speakers.srt`](dub.en.speakers.srt) | synced English captions (plain / with speaker names) |
| [`dubbed-interview-en.mp4`](dubbed-interview-en.mp4) | the video with the English dub and burned-in captions |
| [`youtube-upload-steps.md`](youtube-upload-steps.md) | the YouTube Studio steps written into the upload package |

Prompt: *"Dub interview-es.mp4 into English. Marco and Lucia should keep their own voices. Then make the YouTube upload package."*

The source voices were themselves generated from CMU ARCTIC reference recordings (speakers bdl and slt, free for any use, © Carnegie Mellon University) so the demo contains no real person's voice.
