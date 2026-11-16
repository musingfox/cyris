"""A run alert is worded from the UI catalogs in its own language, and only by the adapter."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import get_args

import pytest

from cyris.adapters.run_alert import alert_message
from cyris.service_layer.alerts import AlertKind, RunAlert

pytestmark = pytest.mark.unit

STARTED = datetime(2026, 11, 16, 0, 5, tzinfo=UTC)
# The issue the alert is about, named as every surface names it, in each catalog's punctuation.
SUFFIX = {"en": ": 11/16 0800 Asia/Taipei", "zh-Hant": "：11/16 0800 Asia/Taipei"}


def _alert(kind: AlertKind, language: str = "en", **values) -> RunAlert:
    return RunAlert(
        kind=kind,
        period="morning",
        started_at=STARTED,
        timezone="Asia/Taipei",
        language=language,
        **values,
    )


@pytest.mark.parametrize(
    ("kind", "language", "headline"),
    [
        ("run_failed", "en", "Digest run failed"),
        ("run_failed", "zh-Hant", "精選產生失敗"),
        ("could_not_start", "en", "Digest run could not start"),
        ("could_not_start", "zh-Hant", "精選無法啟動"),
        ("fetched_nothing", "en", "Digest run fetched nothing"),
        ("fetched_nothing", "zh-Hant", "精選沒抓到任何文章"),
        ("vote_similarity_rejected", "en", "Vote similarity did not run"),
        ("vote_similarity_rejected", "zh-Hant", "投票相似度未執行"),
    ],
)
def test_the_subject_is_the_headline_and_the_hour_in_the_timezone(kind, language, headline) -> None:
    subject, _ = alert_message(_alert(kind, language))

    assert subject == headline + SUFFIX[language]
    assert "morning" not in subject


def test_every_kind_has_a_headline_in_every_catalog() -> None:
    for kind in get_args(AlertKind):
        for language in ("en", "zh-Hant"):
            assert alert_message(_alert(kind, language))[0].endswith(SUFFIX[language])


@pytest.mark.parametrize("kind", ["run_failed", "could_not_start"])
@pytest.mark.parametrize("language", ["en", "zh-Hant"])
def test_an_error_alert_carries_the_raw_error_in_any_language(kind, language) -> None:
    _, body = alert_message(_alert(kind, language, error="RuntimeError: the store is gone"))

    assert body == "RuntimeError: the store is gone"


@pytest.mark.parametrize(
    ("language", "body"),
    [
        ("en", "Failed sources: Feed A, Feed B"),
        ("zh-Hant", "抓取失敗的來源：Feed A, Feed B"),
    ],
)
def test_a_failed_fetch_names_its_sources(language, body) -> None:
    alert = _alert("fetched_nothing", language, sources=("Feed A", "Feed B"))

    assert alert_message(alert)[1] == body


@pytest.mark.parametrize(
    ("language", "body"),
    [
        (
            "en",
            "Vote similarity did not run this period, so reader votes had no effect on this "
            "digest. The digest was published unfiltered. The embedding API refused the "
            "request: HTTP 1403 Forbidden. This repeats every run until the embedding key or "
            "its permissions are fixed.",
        ),
        (
            "zh-Hant",
            "本期未執行投票相似度，讀者的投票沒有作用，精選未經過濾就直接發布。嵌入 API "
            "拒絕了請求：HTTP 1403 Forbidden。在嵌入金鑰或其權限修好之前，每次產生精選都會"
            "再發生。",
        ),
    ],
)
def test_a_rejected_embedding_names_its_status_ungrouped_and_its_detail(language, body) -> None:
    alert = _alert("vote_similarity_rejected", language, status="1403", detail="Forbidden")

    assert alert_message(alert)[1] == body


def test_a_regional_tag_reaches_its_catalog_and_an_unknown_one_is_english() -> None:
    alert = _alert("run_failed")

    assert alert_message(replace(alert, language="zh-TW"))[0] == "精選產生失敗" + SUFFIX["zh-Hant"]
    assert alert_message(replace(alert, language="ja"))[0] == "Digest run failed" + SUFFIX["en"]
