"""Tests for feedback parser."""

import pytest

from cyris.learn.feedback import parse_digest_feedback


class TestParseFeedback:
    def test_valid_digest_with_checkboxes(self, tmp_path):
        """Parse digest with 2 checked and 3 unchecked deep-read."""
        digest = tmp_path / "2026-03-19-morning.md"
        digest.write_text("""
# Digest 2026-03-19 Morning

## Headlines

- **[Article One](https://example.com/1)** — Summary one (TechCrunch)
  - [x] deep-read
- **[Article Two](https://example.com/2)** — Summary two (Stratechery)
  - [ ] deep-read
- **[Article Three](https://example.com/3)** — Summary three (Wired)
  - [x] deep-read
- **[Article Four](https://example.com/4)** — Summary four (Hacker News)
  - [ ] deep-read
- **[Article Five](https://example.com/5)** — Summary five (The Verge)
  - [ ] deep-read
""")

        feedback = parse_digest_feedback(digest)

        assert feedback.digest_date == "2026-03-19"
        assert feedback.digest_period == "morning"
        assert len(feedback.articles) == 5
        assert feedback.liked_count == 2

        # Check specific articles
        assert feedback.articles[0].title == "Article One"
        assert feedback.articles[0].deep_read is True
        assert feedback.articles[0].track is False

        assert feedback.articles[1].deep_read is False
        assert feedback.articles[2].deep_read is True
        assert feedback.articles[2].track is False

    def test_all_unchecked(self, tmp_path):
        """Parse digest with all unchecked boxes."""
        digest = tmp_path / "2026-03-15-evening.md"
        digest.write_text("""
# Digest

- **[Article A](https://test.com/a)** — Summary A (Source A)
  - [ ] deep-read
- **[Article B](https://test.com/b)** — Summary B (Source B)
  - [ ] deep-read
""")

        feedback = parse_digest_feedback(digest)

        assert feedback.liked_count == 0
        assert len(feedback.articles) == 2

    def test_missing_file(self, tmp_path):
        """Raise FileNotFoundError for non-existent file."""
        with pytest.raises(FileNotFoundError, match="not found"):
            parse_digest_feedback(tmp_path / "nonexistent.md")

    def test_invalid_filename_format(self, tmp_path):
        """Raise ValueError for filename without date."""
        digest = tmp_path / "invalid-filename.md"
        digest.write_text("# Digest\n")

        with pytest.raises(ValueError, match="Cannot extract date"):
            parse_digest_feedback(digest)

    def test_section_heading_with_url_checked(self, tmp_path):
        """Parse section heading with URL when deep-read is checked."""
        digest = tmp_path / "2026-03-31-morning.md"
        digest.write_text("""
# Digest 2026-03-31 Morning

## 新聞聚合

### [Tech News](http://a.com)
Multiple tech companies announced layoffs.
`Sources: Reuters, Bloomberg`
- [x] deep-read
- [ ] track

### [AI Updates](http://b.com)
OpenAI releases new model.
`Sources: TechCrunch`
- [ ] deep-read
- [x] track
""")

        feedback = parse_digest_feedback(digest)

        assert len(feedback.articles) == 2
        assert feedback.articles[0].title == "Tech News"
        assert feedback.articles[0].url == "http://a.com"
        assert feedback.articles[0].source == "Reuters, Bloomberg"
        assert feedback.articles[0].deep_read is True
        assert feedback.articles[0].track is False

        assert feedback.articles[1].title == "AI Updates"
        assert feedback.articles[1].url == "http://b.com"
        assert feedback.articles[1].source == "TechCrunch"
        assert feedback.articles[1].deep_read is False
        assert feedback.articles[1].track is True

    def test_section_heading_unchecked_skipped(self, tmp_path):
        """Section heading with no checkboxes should not be added."""
        digest = tmp_path / "2026-03-31-morning.md"
        digest.write_text("""
# Digest 2026-03-31 Morning

### [Unchecked Section](http://c.com)
This section has no checkboxes checked.
`Sources: Example`
- [ ] deep-read
- [ ] track

### [Checked Section](http://d.com)
This one is checked.
`Sources: Another`
- [x] deep-read
- [ ] track
""")

        feedback = parse_digest_feedback(digest)

        assert len(feedback.articles) == 1
        assert feedback.articles[0].title == "Checked Section"
        assert feedback.articles[0].url == "http://d.com"

    def test_section_heading_stops_at_boundary(self, tmp_path):
        """Section heading lookahead should stop at section boundaries."""
        digest = tmp_path / "2026-03-31-morning.md"
        digest.write_text("""
# Digest 2026-03-31 Morning

### [Section One](http://e.com)
First section content.
`Sources: Source1`

### [Section Two](http://f.com)
Second section content.
`Sources: Source2`
- [x] deep-read
- [ ] track
""")

        feedback = parse_digest_feedback(digest)

        # Section One has no checkboxes before boundary, should not be added
        assert len(feedback.articles) == 1
        assert feedback.articles[0].title == "Section Two"
        assert feedback.articles[0].url == "http://f.com"
