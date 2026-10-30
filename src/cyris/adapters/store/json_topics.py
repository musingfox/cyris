"""Topics, their members and tags, and the votes on them, in one json file.

The json backend's home for what D1 keeps in `topics`, `topic_members`, `topic_tags`
and `topic_votes`, with the same rules (`D1TopicStore`).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from cyris.domain.models import TopicRecord, TopicSummary, TopicTag, TopicVote


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class JsonTopicStore:
    """TopicRepository over `topics.json` in the agent vault."""

    def __init__(self, vault_path: Path) -> None:
        self._path = vault_path / "topics.json"

    def _load(self) -> dict:
        if not self._path.exists():
            return {"topics": {}, "members": [], "tags": [], "votes": []}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_topics(
        self, digest_date: str, period: str, topics: list[TopicRecord], seen_at: datetime
    ) -> int:
        stamp = _iso(seen_at)
        data = self._load()
        for t in topics:
            row = data["topics"].setdefault(t.id, {"first_seen": stamp, "last_seen": stamp})
            row["name"] = t.name
            row["last_seen"] = max(row["last_seen"], stamp)
        issue = (digest_date, period)
        data["members"] = [
            m for m in data["members"] if (m["digest_date"], m["period"]) != issue
        ] + [
            {
                "topic_id": t.id,
                "article_url": u,
                "digest_date": digest_date,
                "period": period,
                "seen_at": stamp,
            }
            for t in topics
            for u in t.urls
        ]
        tags = {(g["topic_id"], g["tag"]): g for g in data["tags"]}
        for t in topics:
            for tag in t.tags:
                tags[(t.id, tag.tag)] = {"topic_id": t.id, "tag": tag.tag, "kind": str(tag.kind)}
        kept = {m["topic_id"] for m in data["members"]} | {v["topic_id"] for v in data["votes"]}
        data["topics"] = {i: row for i, row in data["topics"].items() if i in kept}
        data["tags"] = [g for g in tags.values() if g["topic_id"] in kept]
        self._save(data)
        return len(topics) + sum(len(t.urls) + len(t.tags) for t in topics)

    def topics_of_urls(self, urls: list[str]) -> dict[str, str]:
        wanted = set(urls)
        found: dict[str, tuple[str, str]] = {}
        for m in self._load()["members"]:
            url = m["article_url"]
            if url in wanted and (url not in found or m["seen_at"] > found[url][0]):
                found[url] = (m["seen_at"], m["topic_id"])
        return {url: topic for url, (_, topic) in found.items()}

    def record_topic_votes(self, votes: list[TopicVote]) -> int:
        data = self._load()
        have = {(v["topic_id"], v["clicked_at"]) for v in data["votes"]}
        written = 0
        for vote in votes:
            key = (vote.topic_id, _iso(vote.clicked_at))
            if key in have:
                continue
            have.add(key)
            data["votes"].append(
                {"topic_id": key[0], "direction": vote.direction, "clicked_at": key[1]}
            )
            written += 1
        self._save(data)
        return written

    def topic_votes(self) -> list[TopicVote]:
        return [TopicVote.model_validate(v) for v in self._load()["votes"]]

    def recent_topics(self, since: datetime, until: datetime) -> list[TopicSummary]:
        low, high = _iso(since), _iso(until)
        data = self._load()
        ids = sorted(
            (row["last_seen"], i)
            for i, row in data["topics"].items()
            if low <= row["last_seen"] <= high
        )
        return self._summaries(data, [i for _, i in ids])

    def topics_by_ids(self, ids: list[str]) -> list[TopicSummary]:
        data = self._load()
        return self._summaries(data, [i for i in dict.fromkeys(ids) if i in data["topics"]])

    @staticmethod
    def _summaries(data: dict, ids: list[str]) -> list[TopicSummary]:
        tags: dict[str, list[TopicTag]] = {}
        for g in sorted(data["tags"], key=lambda g: g["tag"]):
            tags.setdefault(g["topic_id"], []).append(TopicTag(tag=g["tag"], kind=g["kind"]))
        return [
            TopicSummary(
                id=i,
                name=data["topics"][i]["name"],
                last_seen=data["topics"][i]["last_seen"],
                tags=tags.get(i, []),
            )
            for i in ids
        ]
