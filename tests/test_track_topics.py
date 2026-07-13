"""Tests for load/upsert (top-level + Vault cases for contracts)."""

from datetime import date
from pathlib import Path

import pytest

from cyris.adapters.tracking_yaml import TrackedTopic, load_topics, upsert_topic
from cyris.config import VaultConfigSource


class TestLoadTopics:
    @pytest.mark.asyncio
    async def test_load_from_example_content(self, tmp_path, monkeypatch):
        """T1: load from tracking.example.yaml content -> 2 topics, date type, keywords."""
        example_path = Path(__file__).parent.parent / "tracking.example.yaml"
        content = example_path.read_text(encoding="utf-8")
        tf = tmp_path / "tracking.yaml"
        tf.write_text(content)
        monkeypatch.setattr("cyris.adapters.tracking_yaml.DEFAULT_TRACKING_PATH", tf)

        topics = await load_topics()
        assert len(topics) == 2
        assert topics[0].name == "EU AI Act"
        assert topics[0].created == date(2026, 3, 16)
        assert isinstance(topics[0].created, date)
        assert topics[1].name == "台積電亞利桑那廠"
        assert topics[1].keywords == ["TSMC Arizona", "台積電亞利桑那", "CHIPS Act TSMC"]

    @pytest.mark.asyncio
    async def test_vault_load_nonexistent_returns_empty(self, tmp_path):
        """T2: VaultConfigSource(tmp/'nonexistent').load_topics() -> [] no exception."""
        v = VaultConfigSource(tmp_path / "nonexistent.yaml")
        topics = await v.load_topics()
        assert topics == []

    @pytest.mark.asyncio
    async def test_load_missing_fields_raises_with_path(self, tmp_path, monkeypatch):
        """T3: missing fields -> ValueError msg contains path."""
        tf = tmp_path / "bad.yaml"
        tf.write_text('topics:\n  - name: "X"\n')
        monkeypatch.setattr("cyris.adapters.tracking_yaml.DEFAULT_TRACKING_PATH", tf)

        with pytest.raises(ValueError, match=str(tf)):
            await load_topics()

    @pytest.mark.asyncio
    async def test_load_malformed_yaml_raises(self, tmp_path, monkeypatch):
        """T4: malformed yaml -> ValueError."""
        tf = tmp_path / "bad.yaml"
        tf.write_text("topics: [unclosed")
        monkeypatch.setattr("cyris.adapters.tracking_yaml.DEFAULT_TRACKING_PATH", tf)

        with pytest.raises(ValueError):
            await load_topics()


class TestUpsertTopic:
    @pytest.mark.asyncio
    async def test_upsert_creates_file_bare_date_chinese_no_escape(self, tmp_path, monkeypatch):
        """T1: nonexistent file upsert creates; bare date + raw Chinese; roundtrips."""
        tf = tmp_path / "tracking.yaml"
        monkeypatch.setattr("cyris.adapters.tracking_yaml.DEFAULT_TRACKING_PATH", tf)

        topic = TrackedTopic(
            name="台積電亞利桑那廠",
            keywords=["TSMC Arizona", "台積電亞利桑那"],
            created=date(2026, 3, 10),
        )
        await upsert_topic(topic)

        assert tf.exists()
        content = tf.read_text(encoding="utf-8")
        assert "created: 2026-03-10" in content
        assert "台積電亞利桑那廠" in content
        assert "\\u" not in content

        loaded = await load_topics()
        assert len(loaded) == 1
        assert loaded[0].name == topic.name
        assert loaded[0].keywords == topic.keywords
        assert loaded[0].created == topic.created

    @pytest.mark.asyncio
    async def test_upsert_appends_preserving_prior(self, tmp_path, monkeypatch):
        """T2: existing A + upsert B -> [A, B] order preserved, A unchanged."""
        tf = tmp_path / "tracking.yaml"
        monkeypatch.setattr("cyris.adapters.tracking_yaml.DEFAULT_TRACKING_PATH", tf)

        a = TrackedTopic(
            name="EU AI Act",
            keywords=["EU AI Act", "歐盟 AI 法案"],
            created=date(2026, 3, 16),
        )
        await upsert_topic(a)

        b = TrackedTopic(name="新主題", keywords=["新"], created=date(2026, 3, 17))
        await upsert_topic(b)

        loaded = await load_topics()
        assert len(loaded) == 2
        assert loaded[0].name == "EU AI Act"
        assert loaded[0].keywords == ["EU AI Act", "歐盟 AI 法案"]
        assert loaded[0].created == date(2026, 3, 16)
        assert loaded[1].name == "新主題"

    @pytest.mark.asyncio
    async def test_upsert_replaces_same_name_entire_entry(self, tmp_path, monkeypatch):
        """T3: same name upsert replaces whole entry (keywords updated)."""
        tf = tmp_path / "tracking.yaml"
        monkeypatch.setattr("cyris.adapters.tracking_yaml.DEFAULT_TRACKING_PATH", tf)

        await upsert_topic(
            TrackedTopic(name="EU AI Act", keywords=["old"], created=date(2026, 3, 16))
        )
        await upsert_topic(
            TrackedTopic(name="EU AI Act", keywords=["only-one"], created=date(2026, 3, 16))
        )
        loaded = await load_topics()
        assert len(loaded) == 1
        assert loaded[0].keywords == ["only-one"]

    @pytest.mark.asyncio
    async def test_upsert_bad_yaml_raises_without_modifying_file(self, tmp_path, monkeypatch):
        """T4: bad yaml on upsert -> ValueError, original file untouched."""
        tf = tmp_path / "tracking.yaml"
        bad = "topics: [unclosed"
        tf.write_text(bad)
        monkeypatch.setattr("cyris.adapters.tracking_yaml.DEFAULT_TRACKING_PATH", tf)

        topic = TrackedTopic(name="foo", keywords=["bar"], created=date(2026, 1, 1))
        with pytest.raises(ValueError):
            await upsert_topic(topic)

        assert tf.read_text(encoding="utf-8") == bad


class TestTrackedTopicValidation:
    def test_status_literal_only_active_or_inactive(self):
        """status Literal['active','inactive'] only; bad value -> ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TrackedTopic(name="bad", keywords=[], created=date(2026, 1, 1), status="foo")
        t1 = TrackedTopic(name="ok", keywords=[], created=date(2026, 1, 1), status="active")
        assert t1.status == "active"
        t2 = TrackedTopic(name="ok2", keywords=[], created=date(2026, 1, 1), status="inactive")
        assert t2.status == "inactive"

    def test_extra_fields_forbidden(self):
        """model_config extra='forbid'; unknown field -> ValidationError."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TrackedTopic(name="x", keywords=[], created=date(2026, 1, 1), owner="nick")  # type: ignore[arg-type]
        # covers load path via model_validate
        with pytest.raises(ValidationError):
            TrackedTopic.model_validate(
                {"name": "x", "keywords": [], "created": "2026-01-01", "owner": "nick"}
            )
