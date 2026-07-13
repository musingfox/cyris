"""Parse user feedback from digest markdown files."""

import re
from pathlib import Path

from cyris.domain.models import ArticleFeedback, FeedbackData


def parse_digest_feedback(digest_path: Path) -> FeedbackData:
    """Parse deep-read/track feedback checkboxes from a digest markdown file.

    Extracts [x] deep-read / [x] track checkbox states and maps them
    to ArticleFeedback objects. Used by `cyris articles triage`.

    Args:
        digest_path: Path to the digest markdown file.

    Returns:
        FeedbackData with parsed articles and liked_count.

    Raises:
        FileNotFoundError: If digest file does not exist.
        ValueError: If file format is invalid or unparseable.
    """
    if not digest_path.exists():
        raise FileNotFoundError(f"Digest file not found: {digest_path}")

    content = digest_path.read_text()

    # Extract metadata from frontmatter or filename
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", digest_path.stem)
    if not date_match:
        raise ValueError(f"Cannot extract date from filename: {digest_path.name}")
    digest_date = date_match.group(1)

    period_match = re.search(r"(morning|evening)", digest_path.stem)
    digest_period = period_match.group(1) if period_match else "unknown"

    # Parse article entries with checkboxes
    # Pattern: - **[Title](url)** — summary (Source)
    #          - [ ] deep-read | [ ] track
    articles = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # Match section heading with URL
        section_match = re.match(r"^### \[(.*?)\]\((.*?)\)", line)
        if section_match:
            title = section_match.group(1)
            url = section_match.group(2)

            # Look ahead within next ~10 lines for checkboxes and sources
            deep_read = False
            track = False
            source = ""

            for j in range(i + 1, min(i + 11, len(lines))):
                lookahead = lines[j]

                # Stop at next section boundary
                if re.match(r"^###|^##|^---", lookahead) or re.match(r"^- \*\*", lookahead):
                    break

                # Extract checkbox states
                if re.search(r"\[x\] deep-read", lookahead):
                    deep_read = True
                if re.search(r"\[x\] track", lookahead):
                    track = True

                # Extract sources
                sources_match = re.search(r"`Sources: (.*?)`", lookahead)
                if sources_match:
                    source = sources_match.group(1)

            # Only add if deep_read or track is checked
            if deep_read or track:
                articles.append(
                    ArticleFeedback(
                        title=title,
                        source=source,
                        url=url,
                        deep_read=deep_read,
                        track=track,
                    )
                )
            i += 1
            continue

        # Match article title line (headline format)
        title_match = re.match(r"^- \*\*\[(.*?)\]\((.*?)\)\*\* — .*?\((.*?)\)", line)
        if title_match:
            title = title_match.group(1)
            url = title_match.group(2)
            source = title_match.group(3)

            # Check next line for checkboxes
            if i + 1 < len(lines):
                checkbox_line = lines[i + 1]
                deep_read = bool(re.search(r"\[x\] deep-read", checkbox_line))
                track = bool(re.search(r"\[x\] track", checkbox_line))

                articles.append(
                    ArticleFeedback(
                        title=title,
                        source=source,
                        url=url,
                        deep_read=deep_read,
                        track=track,
                    )
                )
        i += 1

    liked_count = sum(1 for a in articles if a.deep_read)

    return FeedbackData(
        digest_date=digest_date,
        digest_period=digest_period,
        articles=articles,
        liked_count=liked_count,
    )
