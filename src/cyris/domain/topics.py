"""The topics of one issue, as the filter's batches assign them (ADR-0023).

Every article an issue shows belongs to exactly one topic. The filter puts it under a
recent topic, whose id carries across issues, or opens a new one; a topic opened in one
batch is on the list the next batch is offered. An article the filter's model never
placed is a topic of its own, which no other article of its issue can join.
"""

import hashlib
import math
import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from cyris.domain.models import TopicRecord, TopicSummary, TopicTag
from cyris.domain.tags import normalize_tag

# The design's 2-5 tags a topic. A topic takes the tags of each article placed under it,
# so the upper bound is held here; the lower one is only the model's to keep.
MAX_TOPIC_TAGS = 5

# A busy week holds hundreds of topics. Measured with build_filter_prompt on 2026-11-10,
# 300 names of 34 characters are about as long as a full batch's articles at a 500-character
# snippet, so names of that length at most double a batch's prompt; names in Chinese take
# half that, and 300 names at MAX_TOPIC_NAME_LENGTH make it about 4.3 times.
MAX_RECENT_TOPICS = 300

# An upvote lengthens a topic's stay on the recent list, but never past this many lifetimes
# after its last article, so the list holds at most that many days of topics however much
# the reader votes. A starting point (ADR-0026), not a measurement.
MAX_TOPIC_LIFETIMES = 3

# A topic name is a few words, but an unplaced article's raw title becomes one, and a
# feed's title can be a whole paragraph that every later prompt would repeat.
MAX_TOPIC_NAME_LENGTH = 120


def topic_name(raw: str) -> str:
    """The name as it is stored and offered: one line, no control characters, whitespace
    collapsed, at most `MAX_TOPIC_NAME_LENGTH` characters.
    """
    flat = "".join(" " if unicodedata.category(c).startswith("C") else c for c in raw)
    name = " ".join(flat.split())
    if len(name) <= MAX_TOPIC_NAME_LENGTH:
        return name
    return name[: MAX_TOPIC_NAME_LENGTH - 1].rstrip() + "…"


def topic_expiry(last_seen: datetime, pref: float, *, lifetime_days: float) -> datetime:
    """When a topic last seen at `last_seen` leaves the recent list while its net decayed vote
    is `pref`: `lifetime_days` stretched by 2 ** pref, a downvote stretching nothing, and never
    past `MAX_TOPIC_LIFETIMES` lifetimes.
    """
    lifetimes = (
        MAX_TOPIC_LIFETIMES if pref >= math.log2(MAX_TOPIC_LIFETIMES) else 2 ** max(pref, 0.0)
    )
    return last_seen + timedelta(days=lifetime_days * lifetimes)


def recent_expiry(
    recent: Iterable[TopicSummary],
    prefs: Mapping[str, float],
    at: datetime,
    *,
    lifetime_days: float,
) -> dict[str, datetime]:
    """Each of `recent` still on the recent list at `at`, by id, with when it leaves it.

    `prefs` is each voted topic's net vote decayed to `at`, so a vote's stretch fades with it.
    """
    expiry = {
        t.id: topic_expiry(t.last_seen, prefs.get(t.id, 0.0), lifetime_days=lifetime_days)
        for t in recent
    }
    return {tid: leaves for tid, leaves in expiry.items() if at < leaves}


def topic_id(date: str, period: str, name: str) -> str:
    """One name in one issue is one id, so a rerun that opens the topic again keeps it."""
    return _issue_id(date, period, normalize_tag(name) or "")


def own_topic_id(date: str, period: str, url: str) -> str:
    """The id of an article's topic of its own, from its URL exactly as the store keys it:
    `stored_articles.url` compares case, so two URLs differing only in case are two articles.
    """
    return _issue_id(date, period, url)


def _issue_id(date: str, period: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return f"{date}-{period}-{digest}"


class TopicBook:
    """The topics an issue's articles may join, and which one each article joined.

    Of the recent topics, the `MAX_RECENT_TOPICS` that leave the list last are offered in that
    order, and of two leaving together the newest first; every topic this issue opens is offered
    after them. A name joins by the form it is offered in, so a stored name the offer cleans or
    cuts is still joined, and a name two of them share in that form joins the one that stays
    longest, the first listed. `expires` says when each recent topic leaves (`recent_expiry`);
    without it none has a vote, so the newest leaves last. A topic saved again keeps its stored
    tags behind the ones this issue gave it.
    """

    def __init__(
        self,
        recent: Sequence[TopicSummary],
        *,
        date: str,
        period: str,
        expires: Mapping[str, datetime] | None = None,
    ) -> None:
        self._date = date
        self._period = period

        def leaves(t: TopicSummary) -> tuple[datetime, datetime]:
            return (t.last_seen if expires is None else expires[t.id], t.last_seen)

        kept = sorted(recent, key=leaves, reverse=True)[:MAX_RECENT_TOPICS]
        self._listed = [TopicRecord(id=t.id, name=topic_name(t.name), urls=[]) for t in kept]
        self._by_name: dict[str, TopicRecord] = {}
        for topic in self._listed:
            self._by_name.setdefault(normalize_tag(topic.name), topic)
        self._stored_tags = {t.id: t.tags for t in recent}
        self._of_url: dict[str, TopicRecord] = {}

    def names(self) -> list[str]:
        """The topic names on offer; a name's position is how the model picks its topic."""
        return [t.name for t in self._listed]

    def holds(self, url: str) -> bool:
        return url in self._of_url

    def place(self, url: str, *, position: int | None, name: str, tags: Iterable[TopicTag]) -> None:
        """Put the article under the listed topic at `position`, or else the one called `name`,
        opening it if no listed topic is. An article already placed stays where it is, and a
        blank name places nothing, leaving the article to a topic of its own.
        """
        if url in self._of_url or (position is None and not topic_name(name)):
            return
        topic = self._listed[position] if position is not None else self._named(name)
        self._of_url[url] = topic
        known = {t.tag for t in topic.tags}
        for tag in tags:
            if tag.tag not in known and len(topic.tags) < MAX_TOPIC_TAGS:
                known.add(tag.tag)
                topic.tags.append(tag)

    def ensure(self, url: str, name: str) -> TopicRecord:
        """The article's topic, or a new one of its own when nothing placed it."""
        if url not in self._of_url:
            self._of_url[url] = TopicRecord(
                id=own_topic_id(self._date, self._period, url), name=topic_name(name), urls=[]
            )
        return self._of_url[url]

    def records(self, shown: Collection[str]) -> list[TopicRecord]:
        """The topics holding an article the issue shows, each with only those articles."""
        held: dict[str, TopicRecord] = {}
        for url, topic in self._of_url.items():
            if url in shown:
                held.setdefault(topic.id, topic.model_copy(update={"urls": []})).urls.append(url)
        return [self._with_stored_tags(topic) for topic in held.values()]

    def _with_stored_tags(self, topic: TopicRecord) -> TopicRecord:
        """This run's tags first, then the stored ones they do not repeat, up to the cap."""
        known = {t.tag for t in topic.tags}
        kept = [t for t in self._stored_tags.get(topic.id, ()) if t.tag not in known]
        return topic.model_copy(update={"tags": [*topic.tags, *kept][:MAX_TOPIC_TAGS]})

    def _named(self, raw: str) -> TopicRecord:
        name = topic_name(raw)
        key = normalize_tag(name)
        if key not in self._by_name:
            topic = TopicRecord(id=topic_id(self._date, self._period, name), name=name, urls=[])
            self._listed.append(topic)
            self._by_name[key] = topic
        return self._by_name[key]
