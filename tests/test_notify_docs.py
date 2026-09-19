"""The Discord webhook's docs name the home a run actually reads."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
CLAUDE_MD = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def _section(doc: str, start: str, end: str) -> str:
    return doc.split(start, 1)[1].split(end, 1)[0]


def _discord_webhook_row(section: str) -> str:
    return next(line for line in section.splitlines() if line.startswith("| Discord webhook "))


def test_section_5_grades_the_webhook_d_in_settings() -> None:
    section = _section(ARCHITECTURE, "## 5.", "## 6.")
    row = _discord_webhook_row(section)
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[1] == "D"
    assert cells[1] != "C"
    assert "`settings`" in row
    assert "/settings" in row
    assert "CYRIS_DISCORD_WEBHOOK_URL" not in row
    assert "cannot turn notifications off" not in row


def _row(section: str, first_cell: str) -> str:
    return next(line for line in section.splitlines() if line.startswith(f"| {first_cell} "))


def test_section_4_names_no_fallback_for_sources_or_settings() -> None:
    section = _section(ARCHITECTURE, "## 4.", "## 5.")
    for datum in ("Source definitions", "Runtime settings"):
        assert "fallback" not in _row(section, datum), datum


def test_section_5_names_provider_none() -> None:
    section = _section(ARCHITECTURE, "## 5.", "## 6.")
    assert '"none"' in _row(section, "LLM provider + model")


def test_section_7_closes_17_and_drops_the_per_key_origin_step() -> None:
    section = _section(ARCHITECTURE, "## 7.", "## 8.")
    assert "per-key origin" not in _row(section, "34")
    assert _row(section, "~~17~~").startswith("| ~~17~~ | ~~")


def test_claude_md_names_settings_push_and_no_bundled_feed_list() -> None:
    assert "cyris settings push" in CLAUDE_MD
    assert "bundled src/feeds.json" not in CLAUDE_MD


def test_unreleased_names_settings_push_provider_none_and_the_retired_variable() -> None:
    unreleased = _section(CHANGELOG, "## [Unreleased]", "## [")
    assert "cyris settings push" in unreleased
    assert 'provider = "none"' in unreleased
    removed = _section(unreleased, "### Removed", "\n### ")
    assert "CYRIS_DISCORD_WEBHOOK_URL" in removed


def test_section_4_puts_the_webhook_in_settings() -> None:
    section = _section(ARCHITECTURE, "## 4.", "## 5.")
    rows = [line for line in section.splitlines() if line.startswith("|")]
    assert any("Discord webhook" in row and "settings" in row for row in rows)


def test_section_7_records_the_webhook_landing_in_settings() -> None:
    section = _section(ARCHITECTURE, "## 7.", "## 8.")
    assert "Discord webhook" in section
    assert "`settings`" in section
    assert any(
        "Discord webhook" in block and "`settings`" in block for block in section.split("\n\n")
    )


def test_api_keys_on_the_settings_page_stay_undecided() -> None:
    section = _section(ARCHITECTURE, "## 5.", "## 6.")
    row = next(line for line in section.splitlines() if "API keys on the settings page" in line)
    assert "undecided" in row


def test_env_example_no_longer_offers_the_webhook_variable() -> None:
    assert "CYRIS_DISCORD_WEBHOOK_URL" not in ENV_EXAMPLE


def test_deploy_facing_copy_does_not_name_the_old_table() -> None:
    for rel in (".env.example", "workers/app/README.md", "CLAUDE.md"):
        assert "[general.notify]" not in (ROOT / rel).read_text(encoding="utf-8")


def test_unreleased_names_the_webhook_on_settings_and_keeps_the_shipped_bullet() -> None:
    unreleased = _section(CHANGELOG, "## [Unreleased]", "## [")
    assert "Discord webhook" in unreleased
    assert "/settings" in unreleased
    assert (
        "- `CYRIS_DISCORD_WEBHOOK_URL` — the webhook no longer has to live in `cyris.toml`."
        in CHANGELOG
    )


def test_section_2_still_injects_notify_directly() -> None:
    section = _section(ARCHITECTURE, "## 2.", "## 3.")
    row = next(line for line in section.splitlines() if "`notify`" in line)
    assert row == (
        "| **Direct injection** (no Protocol) | `HtmlDigestWriter`, `publish`, "
        "`sync_promotions`, `append_usage`, `notify`, `D1TagStore`, `D1StoryStore` | "
        "**Medium** — the core calls them directly; a second backend needs a Protocol first |"
    )
