"""A synthetic Substack-shaped feed body: a long picture block, then the paragraphs.

Both feed adapters must hand the pipeline the first paragraph, not the markup
that precedes it (`tests/test_rss_worker_source.py`, `tests/test_rss_source.py`).
"""

FIRST_SENTENCE = "A copier repeats what it was shown, and a craftsperson answers the room."
SRCSET = "".join(
    f'<source type="image/webp" srcset="https://img.test/w_{w}.webp {w}w">'
    for w in range(100, 1500, 25)
)
CAPTIONED_IMAGE = (
    '<div class="captioned-image-container"><figure>'
    '<a class="image-link" target="_blank" href="https://img.test/full.png"><picture>'
    + SRCSET
    + '<img src="https://img.test/w_1456.png" alt="" width="1456" height="819">'
    "</picture></a></figure></div>"
)

POST_HTML = (
    CAPTIONED_IMAGE
    + f"<p>{FIRST_SENTENCE} It keeps the form and loses the reason.</p>"
    + "<p>The second paragraph says why the reason matters more than the form.</p>"
)
