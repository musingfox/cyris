---
id: youtube-content-not-scraped
status: proposed
scope:
  - "pyproject.toml"
  - "src/cyris/adapters/*"
  - "src/cyris/service_layer/*"
  - "workers/*/src/*.js"
verify: check:! grep -rqiE "youtube[-_]transcript|youtubei/v1|timedtext" pyproject.toml uv.lock src workers/*/src
related: [media-condensation-falls-back-to-announcement]
source: multimedia-sources-podcast-youtube
adr: null
---

A YouTube video's content reaches cyris only through a documented API — today
Gemini's YouTube-URL input — never through YouTube's undocumented endpoints or a
library built on them.

The condenser passes the watch URL as a `file_data.file_uri` part to Gemini's
`generateContent`; one call on 2026-09-28 (`gemini-3.8-flash`, a 51:55 video)
returned HTTP 200 in 43 s with 283,511 input tokens. Captions are no substitute:
the YouTube Data API's `captions.download` requires edit rights on the video.

Out of scope: discovering uploads from the channel feed
(`feeds/videos.xml?channel_id=`), which is the documented WebSub topic URL and
carries no transcript. The rule names no provider beyond "documented".

A violation is silent because the fallback hides it. `youtube-transcript-api`
and other innertube clients work from a laptop, and their own README says
YouTube blocks most cloud-provider IPs; in the container every call fails, each
video falls back to its description, and the digest looks normal while no video
is ever condensed. Automated access of this kind is also outside YouTube's terms.

The check fails on the library's name, the innertube path or the caption
endpoint anywhere in the dependency files, `src/` or a Worker's `src/`. It was
green on 2026-09-28 and went red on a scratch file importing
`youtube_transcript_api`.
