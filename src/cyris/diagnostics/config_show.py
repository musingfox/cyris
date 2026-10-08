"""Every graded setting, the value a run would use, and where that value came from.

Information, not a judgement: no value is checked here, which is `doctor`'s job.
The values are the loaded Config's own. The source of each is read off what the
load recorded (`Config.file_toml`, `Config.dotenv_names`) under the precedence
config.py applies; nothing is read from a file or from D1 a second time.
"""

import importlib
import json
import os
from dataclasses import dataclass
from functools import reduce
from importlib.resources import files
from typing import Any

from cyris.adapters.notify import mask_discord_webhook_url
from cyris.bootstrap import default_models, embedding_defaults, embedding_threshold
from cyris.config import B_GRADE_ENV_VARS, GRADE_D_KEYS, Config

_REGISTRY: dict[str, Any] = json.loads(
    (files("cyris.diagnostics") / "config_show.json").read_text(encoding="utf-8")
)
SOURCES: dict[str, str] = _REGISTRY["sources"]
SECRETS: tuple[str, ...] = tuple(_REGISTRY["secrets"])
IDENTITY: dict[str, str] = {**B_GRADE_ENV_VARS, **_REGISTRY["identity"]}
_NONE = _REGISTRY["values"]["none"]

# A style prompt or a webhook URL overflows its own row instead of pushing every
# other row's source column off the screen.
_VALUE_WIDTH_CAP = 48


@dataclass(frozen=True)
class Row:
    grade: str
    key: str
    value: str
    source: str


def config_rows(cfg: Config) -> list[Row]:
    """One row per graded setting, grade A first."""
    return [*_baked(cfg), *_identity(cfg), *_secrets(cfg), *_runtime(cfg)]


def render(rows: list[Row]) -> str:
    """Plain aligned columns, grouped by grade, for pasting into a support chat."""
    header = _REGISTRY["header"]
    key_width = max(len(text) for text in [header[0], *(r.key for r in rows)])
    value_width = min(
        _VALUE_WIDTH_CAP, max(len(text) for text in [header[1], *(r.value for r in rows)])
    )

    def line(key: str, value: str, source: str) -> str:
        return f"{key:<{key_width}}  {value:<{value_width}}  {source}"

    lines = [line(*header)]
    for grade, title in _REGISTRY["grades"].items():
        lines += ["", title]
        lines += [line(r.key, r.value, r.source) for r in rows if r.grade == grade]
    return "\n".join(lines)


def _shown(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _env_source(cfg: Config, name: str) -> str:
    if not os.environ.get(name):
        return SOURCES["unset"]
    return SOURCES["dotenv"] if name in cfg.dotenv_names else SOURCES["environment"]


def _env_row(cfg: Config, grade: str, key: str, name: str, shown: str) -> Row:
    source = _env_source(cfg, name)
    return Row(grade, key, _NONE if source == SOURCES["unset"] else shown, source)


def _baked(cfg: Config) -> list[Row]:
    rows = []
    for key, ref in _REGISTRY["baked"].items():
        module, attribute = ref.split(":")
        value = reduce(getattr, attribute.split("."), importlib.import_module(module))
        rows.append(Row("A", key, _shown(value), SOURCES["code"]))
    rows.append(_threshold_row(cfg))
    for name in _REGISTRY["baked_env"]:
        rows.append(_env_row(cfg, "A", name, name, _shown(os.environ.get(name, ""))))
    return rows


def _threshold_row(cfg: Config) -> Row:
    key = "vote_similarity.threshold"
    vote = cfg.app.vote_similarity
    if vote is None:
        return Row("A", key, _NONE, SOURCES["missing"])
    if vote.threshold is not None:
        return Row("A", key, _shown(vote.threshold), SOURCES["file"])
    threshold = embedding_threshold(cfg)
    if threshold is None:
        return Row("A", key, _NONE, SOURCES["missing"])
    return Row("A", key, _shown(threshold), SOURCES["default"])


def _identity(cfg: Config) -> list[Row]:
    """Grade B: a non-empty file value wins, else the environment, else the model's default."""
    rows = []
    for key, name in IDENTITY.items():
        table, field = key.split(".", 1)
        value = getattr(getattr(cfg.app, table), field)
        body = cfg.file_toml.get(table)
        if isinstance(body, dict) and body.get(field, "") != "":
            source = SOURCES["file"]
        elif os.environ.get(name):
            source = _env_source(cfg, name)
        else:
            source = SOURCES["code"]
        rows.append(Row("B", key, _shown(value), source))
    return rows


def _secrets(cfg: Config) -> list[Row]:
    """Grade C: whether each is set and from where. Never any part of its value."""
    return [_env_row(cfg, "C", name, name, _REGISTRY["values"]["set"]) for name in SECRETS]


def _runtime(cfg: Config) -> list[Row]:
    home = SOURCES["d1"] if cfg.app.store.is_d1 else SOURCES["file"]
    values = cfg.present_settings()
    rows = []
    for key in GRADE_D_KEYS:
        if key not in values:
            rows.append(Row("D", key, _NONE, SOURCES["missing"]))
            continue
        value, source = values[key], home
        if value == "" and (fallback := _default_model(key, values)):
            value, source = fallback, SOURCES["default"]
        if key == "notify.discord_webhook_url":
            value = mask_discord_webhook_url(value)  # a posting token, not a secret by grade
        rows.append(Row("D", key, _shown(value), source))
    return rows


def _default_model(key: str, values: dict[str, Any]) -> str | None:
    """What an empty model runs as: bootstrap.build_llm and make_embedder fill it the same way."""
    if key == "llm_provider.model":
        return default_models().get(values.get("llm_provider.provider", ""))
    if key == "vote_similarity.model" and "vote_similarity.provider" in values:
        return embedding_defaults(values["vote_similarity.provider"])["model"]
    return None
