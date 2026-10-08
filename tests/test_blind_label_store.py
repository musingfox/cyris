"""D1 `blind_labels`: the filter pool, the drawn sample, and each label against it."""

import json
from datetime import UTC, datetime

import pytest
from fakes import CountingD1, SqliteD1

from cyris.adapters.store.blind_labels import D1BlindLabels, SampleRow
from cyris.domain.models import ArticleState

pytestmark = pytest.mark.unit

SINCE = datetime(2026, 9, 18, 10, tzinfo=UTC)
AFTER = "2026-09-19T00:00:00.000000+00:00"
BEFORE = "2026-09-18T09:59:59.000000+00:00"
DRAWN = datetime(2026, 10, 8, 12, tzinfo=UTC)


def put(
    db: SqliteD1,
    url: str,
    *,
    state: str = "accepted",
    tier: str = "filter",
    tags: tuple[str, ...] = (),
    seen: str = AFTER,
    reason: str | None = None,
    triaged_at: str | None = None,
    title: str = "",
    content: str = "",
) -> None:
    db.query(
        "INSERT INTO stored_articles (url, title, content, published_at, source_name, "
        "source_tier, source_tags, state, first_seen_at, rejection_reason, triaged_at) "
        "VALUES (?, ?, ?, ?, 'Source', ?, ?, ?, ?, ?, ?)",
        [
            url,
            title or url,
            content,
            seen,
            tier,
            json.dumps(list(tags)),
            state,
            seen,
            reason,
            triaged_at,
        ],
    )


def row(url: str, position: int, state=ArticleState.ACCEPTED, *, news=False, size=10):
    return SampleRow(url=url, position=position, news=news, pipeline_state=state, stratum_size=size)


class TestPool:
    def test_takes_filter_rows_since_the_cutoff_that_no_human_stamped(self) -> None:
        db = SqliteD1()
        put(db, "kept")
        put(db, "pending", state="pending")
        put(db, "filtered", state="rejected", reason="filtered")
        put(db, "early", seen=BEFORE)
        put(db, "summarize", tier="summarize")
        put(db, "voted", state="rejected", reason="not_interested", triaged_at=AFTER)

        pool = D1BlindLabels(db).pool(SINCE)

        assert [(r.url, r.state) for r in pool] == [
            ("filtered", ArticleState.REJECTED),
            ("kept", ArticleState.ACCEPTED),
            ("pending", ArticleState.PENDING),
        ]

    def test_a_rejection_the_filter_did_not_make_is_left_out(self) -> None:
        db = SqliteD1()
        put(db, "other", state="rejected", reason="already_known")
        put(db, "awaiting", state="awaiting_triage")

        assert D1BlindLabels(db).pool(SINCE) == []

    def test_news_is_the_canonical_tag_among_the_source_tags(self) -> None:
        db = SqliteD1()
        put(db, "a", tags=("tech", "news"))
        put(db, "b", tags=("newsletter",))

        assert [(r.url, r.news) for r in D1BlindLabels(db).pool(SINCE)] == [
            ("a", True),
            ("b", False),
        ]


class TestSample:
    def test_a_saved_sample_records_its_frozen_verdict_seed_and_cutoff(self) -> None:
        db = SqliteD1()
        labels = D1BlindLabels(db)

        labels.replace_sample(
            [row("u1", 0, ArticleState.REJECTED, news=True, size=7)],
            seed=3,
            since=SINCE,
            drawn_at=DRAWN,
        )

        assert db.query("SELECT * FROM blind_labels").rows == [
            {
                "url": "u1",
                "position": 0,
                "news": 1,
                "pipeline_state": "rejected",
                "stratum_size": 7,
                "seed": 3,
                "pool_since": "2026-09-18T10:00:00.000000+00:00",
                "drawn_at": "2026-10-08T12:00:00.000000+00:00",
                "label": None,
                "labeled_at": None,
            }
        ]
        assert labels.size() == 1

    def test_replacing_drops_the_previous_sample(self) -> None:
        db = SqliteD1()
        labels = D1BlindLabels(db)
        labels.replace_sample([row("old", 0)], seed=1, since=SINCE, drawn_at=DRAWN)

        labels.replace_sample([row("new", 0)], seed=2, since=SINCE, drawn_at=DRAWN)

        assert [r["url"] for r in db.query("SELECT url FROM blind_labels").rows] == ["new"]

    def test_a_hundred_rows_are_written_in_batches_under_the_parameter_budget(self) -> None:
        db = CountingD1()

        D1BlindLabels(db).replace_sample(
            [row(f"u{i}", i) for i in range(100)], seed=1, since=SINCE, drawn_at=DRAWN
        )

        # One DELETE, then eight parameters a row: twelve rows a statement.
        assert db.query_count == 1 + 9


def sampled(db: SqliteD1, *urls: str) -> D1BlindLabels:
    labels = D1BlindLabels(db)
    labels.replace_sample(
        [row(url, i) for i, url in enumerate(urls)], seed=1, since=SINCE, drawn_at=DRAWN
    )
    return labels


class TestCards:
    def test_the_next_card_is_the_lowest_unanswered_position(self) -> None:
        db = SqliteD1()
        put(db, "a", title="A", content="<p>Body</p>")
        put(db, "b", title="B")
        labels = sampled(db, "a", "b")

        first = labels.next_card()
        labels.record("a", "skip", DRAWN)
        second = labels.next_card()

        assert (first.url, first.title, first.source, first.content) == (
            "a",
            "A",
            "Source",
            "<p>Body</p>",
        )
        assert second.url == "b"

    def test_no_card_once_every_item_is_answered(self) -> None:
        db = SqliteD1()
        put(db, "a")
        labels = sampled(db, "a")
        labels.record("a", "up", DRAWN)

        assert labels.next_card() is None
        assert labels.progress() == (1, 1)

    def test_progress_counts_every_answer_against_the_sample(self) -> None:
        db = SqliteD1()
        labels = sampled(db, "a", "b", "c")
        labels.record("a", "down", DRAWN)

        assert labels.progress() == (1, 3)


class TestRecord:
    def test_an_open_item_takes_one_label_and_its_time(self) -> None:
        db = SqliteD1()
        labels = sampled(db, "a")

        assert labels.is_open("a")
        assert labels.record("a", "up", DRAWN) is True
        assert not labels.is_open("a")
        assert db.query("SELECT label, labeled_at FROM blind_labels").rows == [
            {"label": "up", "labeled_at": "2026-10-08T12:00:00.000000+00:00"}
        ]

    def test_an_answered_item_keeps_its_first_label(self) -> None:
        db = SqliteD1()
        labels = sampled(db, "a")
        labels.record("a", "up", DRAWN)

        assert labels.record("a", "down", DRAWN) is False
        assert db.query("SELECT label FROM blind_labels").rows == [{"label": "up"}]

    def test_a_url_outside_the_sample_is_not_open(self) -> None:
        labels = sampled(SqliteD1(), "a")

        assert not labels.is_open("elsewhere")
        assert labels.record("elsewhere", "up", DRAWN) is False

    def test_the_table_refuses_a_label_that_is_not_up_down_or_skip(self) -> None:
        labels = sampled(SqliteD1(), "a")

        with pytest.raises(Exception, match="CHECK"):
            labels.record("a", "maybe", DRAWN)


class TestReportReads:
    def test_labeled_rows_are_the_up_and_down_answers_with_their_frozen_verdict(self) -> None:
        db = SqliteD1()
        put(db, "a", title="A")
        put(db, "b", title="B")
        put(db, "c", title="C")
        labels = D1BlindLabels(db)
        labels.replace_sample(
            [
                row("a", 0, ArticleState.REJECTED, news=True, size=4),
                row("b", 1),
                row("c", 2),
            ],
            seed=1,
            since=SINCE,
            drawn_at=DRAWN,
        )
        labels.record("a", "up", DRAWN)
        labels.record("b", "skip", DRAWN)

        assert [
            (r.url, r.title, r.news, r.pipeline_state, r.stratum_size, r.up)
            for r in labels.labeled()
        ] == [("a", "A", True, ArticleState.REJECTED, 4, True)]

    def test_human_votes_leave_out_every_sampled_url(self) -> None:
        db = SqliteD1()
        put(db, "seed-up", title="Up", triaged_at=AFTER)
        put(db, "seed-down", title="Down", state="rejected", triaged_at=AFTER)
        put(db, "pipeline", title="Pipeline")
        put(db, "labeled", title="Labeled", triaged_at=AFTER)
        sampled(db, "labeled")

        votes = D1BlindLabels(db).human_votes()

        assert sorted((v.url, v.title, v.up) for v in votes) == [
            ("seed-down", "Down", False),
            ("seed-up", "Up", True),
        ]
