from cyris.domain.tags import normalize_tag, normalize_tags


def test_normalize_tag_collapses_whitespace():
    assert normalize_tag("  Machine  Learning ") == "machine learning"


def test_normalize_tag_converts_fullwidth_characters():
    assert normalize_tag("ＡＩ") == "ai"


def test_normalize_tag_drops_empty_values():
    assert normalize_tag("   ") is None


def test_normalize_tags_deduplicates_in_order():
    assert normalize_tags(["AI", "ai", "LLM"]) == ["ai", "llm"]


def test_normalize_tags_ignores_non_strings():
    assert normalize_tags(["Rust", 42, None]) == ["rust"]
