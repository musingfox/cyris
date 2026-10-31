"""Feed entries naming a story image, and the URL each must give, on both feed parsers.

`workers/rss/src/parse.js` buffers feeds for the Worker path and `RssSource` polls
them directly; one entry must give one image URL on either path. Each case is the
inside of an RSS `<item>` whose link is `LINK`, and the URL wanted from it.
"""

LINK = "https://d.test/post/1"
DATE = "Tue, 18 Mar 2026 10:00:00 GMT"

NAMESPACES = (
    'xmlns:media="http://search.yahoo.com/mrss/"'
    ' xmlns:content="http://purl.org/rss/1.0/modules/content/"'
)

CASES: dict[str, tuple[str, str | None]] = {
    "media:thumbnail before any other": (
        '<media:thumbnail url="https://cdn.d.test/thumb.jpg"/>'
        '<media:content url="https://cdn.d.test/content.jpg" medium="image"/>'
        '<enclosure url="https://cdn.d.test/enc.jpg" type="image/jpeg" length="1"/>'
        '<content:encoded><![CDATA[<p><img src="https://cdn.d.test/body.jpg"></p>]]>'
        "</content:encoded>",
        "https://cdn.d.test/thumb.jpg",
    ),
    "media:content that says image": (
        '<media:content url="https://cdn.d.test/talk.mp3" type="audio/mpeg"/>'
        '<media:content url="https://cdn.d.test/content.png" type="image/png"/>'
        '<enclosure url="https://cdn.d.test/enc.jpg" type="image/jpeg" length="1"/>',
        "https://cdn.d.test/content.png",
    ),
    "media:content that does not say": (
        '<media:content url="https://cdn.d.test/clip.mp4"/>',
        None,
    ),
    "media:group read as the entry": (
        '<media:group><media:thumbnail url="https://i.d.test/hq.jpg"/></media:group>',
        "https://i.d.test/hq.jpg",
    ),
    "image enclosure before content": (
        '<enclosure url="https://cdn.d.test/enc.webp" type="image/webp" length="1"/>'
        '<description><![CDATA[<img src="https://cdn.d.test/body.jpg">]]></description>',
        "https://cdn.d.test/enc.webp",
    ),
    "audio enclosure": (
        '<enclosure url="https://cdn.d.test/ep.mp3" type="audio/mpeg" length="1"/>',
        None,
    ),
    "first img in content, resolved against the link": (
        '<content:encoded><![CDATA[<p>Text</p><img class="a" src="/media/one.jpg?w=1&amp;h=2">'
        '<img src="https://cdn.d.test/two.jpg">]]></content:encoded>',
        "https://d.test/media/one.jpg?w=1&h=2",
    ),
    "escaped img in description": (
        '<description>&lt;img src="https://cdn.d.test/escaped.jpg"&gt; body</description>',
        "https://cdn.d.test/escaped.jpg",
    ),
    "media:thumbnail nested in a video media:content": (
        '<media:content url="https://cdn.d.test/clip.mp4" type="video/mp4">'
        '<media:thumbnail url="https://cdn.d.test/poster.jpg"/></media:content>',
        "https://cdn.d.test/poster.jpg",
    ),
    "malformed image address": (
        '<media:thumbnail url="http://[invalid/image.jpg"/>',
        None,
    ),
    "none": ("<description>no picture here</description>", None),
    "data: img only": (
        '<description><![CDATA[<img src="data:image/gif;base64,R0lGOD">]]></description>',
        None,
    ),
}


def feed(inner: str) -> str:
    """One RSS feed holding one item around `inner`."""
    return (
        f'<?xml version="1.0"?><rss version="2.0" {NAMESPACES}><channel><title>F</title>'
        f"<item><title>Pictured</title><link>{LINK}</link><pubDate>{DATE}</pubDate>"
        f"{inner}</item></channel></rss>"
    )
