# Upload these dubs to YouTube

YouTube's public Data API cannot add audio tracks, so this last step happens in
YouTube Studio (about one minute per language). Multi-language audio is available
to channels that YouTube has enabled for it.

1. Open https://studio.youtube.com, choose **Content**, then the video.
2. Open **Languages** in the left menu. Set the video language if it is not set yet.
3. For each folder below, click **Add language**, choose the language, then:
   - **Dub** -> **Add** -> upload `audio.<ext>` from that folder -> **Publish**.
   - **Subtitles** -> **Add** -> **Upload file** -> *With timing* -> `captions.srt`.
   - **Title & description** -> **Add** -> paste `title.txt` and `description.txt` (when present).
4. Viewers pick the track under **Settings (gear) -> Audio track**.

Audio tracks must match the video length (these do) and contain only the dub.
