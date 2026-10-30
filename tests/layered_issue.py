"""One issue holding every block the page draws, for the mail and Discord tests.

A tracked hit, a read and a followed pebble, a two-article topic (sand), a one-article
topic and a headline (water). Each source name is unique, so a test can tell where it shows.
"""

from cyris.domain.models import DigestContent, DigestItem, DigestSection

# The sources the page names only inside a sand block's member list, or nowhere.
UNNAMED_SOURCES = ("SRC-WIRE-a", "SRC-WIRE-b", "SRC-LONE", "SRC-HEADLINE", "SRC-TRACKED")


def four_layer_issue() -> DigestContent:
    members = [
        DigestItem(
            title=f"Report {n}",
            summary=f"Excerpt {n}",
            sources=[f"SRC-WIRE-{n}"],
            urls=[f"https://wire-{n}.example.test/{n}"],
            passed_through=True,
        )
        for n in "ab"
    ]
    lone = DigestItem(
        title="One report",
        summary="Its summary.",
        sources=["SRC-LONE"],
        urls=["https://lone.example.test/1"],
    )
    return DigestContent(
        date="2026-10-29",
        period="morning",
        sources_processed=4,
        articles_received=7,
        articles_included=6,
        featured_articles=[
            DigestSection(
                heading="Read",
                items=[
                    DigestItem(
                        title="A long read",
                        summary="Why it matters.",
                        sources=["SRC-PEBBLE"],
                        urls=["https://pebble.example.test/read"],
                    )
                ],
            )
        ],
        fan_sections=[
            DigestSection(
                heading="SRC-FAN",
                items=[
                    DigestItem(
                        title="A followed post",
                        summary="",
                        sources=["SRC-FAN"],
                        urls=["https://fan.example.test/post"],
                    )
                ],
            )
        ],
        news_clusters=[
            DigestSection(
                heading="Two reports of one event",
                story_id="2026-10-29-morning-ab",
                members=members,
                items=[
                    DigestItem(
                        title="Two reports of one event",
                        summary="The model's cluster summary.",
                        sources=[m.sources[0] for m in members],
                        urls=[m.urls[0] for m in members],
                    )
                ],
            ),
            DigestSection(heading="One report", items=[lone], members=[lone]),
        ],
        filtered_headlines=[
            DigestItem(
                title="A headline",
                summary="The filter's sentence.",
                sources=["SRC-HEADLINE"],
                urls=["https://headline.example.test/1"],
            )
        ],
        tracked_topics=[
            DigestSection(
                heading="Tracked topic",
                items=[
                    DigestItem(
                        title="A tracked hit",
                        summary="Not shown.",
                        sources=["SRC-TRACKED"],
                        urls=["https://tracked.example.test/hit"],
                    )
                ],
            )
        ],
    )
