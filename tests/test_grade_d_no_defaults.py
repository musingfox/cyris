"""No grade-D setting has a value in code: a table missing a key is left unbuilt."""

import inspect
from pathlib import Path

import pytest
from fakes import TEST_SETTINGS, SqliteD1, make_config, settings_toml
from pydantic import ValidationError
from typer.testing import CliRunner

from cyris.config import (
    GRADE_D_KEYS,
    AppConfig,
    DigestConfig,
    GeneralConfig,
    LLMProviderConfig,
    NotifyConfig,
    RoutingConfig,
    VoteSimilarityConfig,
    load_config,
    validate_setting,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parent.parent
TABLES = {
    "general": GeneralConfig,
    "notify": NotifyConfig,
    "llm_provider": LLMProviderConfig,
    "digest": DigestConfig,
    "routing": RoutingConfig,
    "vote_similarity": VoteSimilarityConfig,
}


def _table_values(table: str) -> dict:
    return {
        key.split(".", 1)[1]: value
        for key, value in TEST_SETTINGS.items()
        if key.split(".", 1)[0] == table
    }


@pytest.mark.parametrize("key", GRADE_D_KEYS)
def test_every_runtime_setting_is_required_in_its_table(key):
    table, field = key.split(".", 1)
    assert TABLES[table].model_fields[field].is_required() is True


@pytest.mark.parametrize("key", GRADE_D_KEYS)
def test_a_table_without_one_setting_does_not_build(key):
    table, field = key.split(".", 1)
    values = _table_values(table)
    del values[field]

    with pytest.raises(ValidationError) as exc:
        TABLES[table].model_validate(values)

    assert (field,) in [err["loc"] for err in exc.value.errors()]


def test_an_app_config_needs_every_settings_table():
    with pytest.raises(ValidationError) as exc:
        AppConfig.model_validate({})

    locations = {err["loc"][0] for err in exc.value.errors()}
    assert set(TABLES) <= locations


def test_a_json_file_missing_one_key_leaves_only_that_table_unbuilt(tmp_path):
    (tmp_path / "cyris.toml").write_text(settings_toml(omit=["digest.style_prompt"]))

    cfg = load_config(tmp_path / "cyris.toml", tmp_path / "sources.yaml")

    assert cfg.app.digest is None
    assert cfg.app.general.timezone == TEST_SETTINGS["general.timezone"]
    assert cfg.missing_settings == ["digest.style_prompt"]


@pytest.fixture
def empty_d1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A D1 deployment on a fresh sqlite with no tables; returns its cyris.toml."""
    db = SqliteD1(with_schema=False)
    # Patched by name: see test_doctor.
    monkeypatch.setattr("cyris.adapters.store.d1.D1Client", lambda **_kw: db)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.delenv("CYRIS_PROMOTE_TOKEN", raising=False)
    config = tmp_path / "cyris.toml"
    config.write_text('[store]\nbackend = "d1"\ndatabase_id = "db"\n')
    return config


def test_an_empty_d1_leaves_every_settings_table_unbuilt(tmp_path, empty_d1):
    from cyris.bootstrap import load_effective_config

    cfg = load_effective_config(empty_d1, tmp_path / "sources.yaml")

    assert [getattr(cfg.app, table) for table in TABLES] == [None] * len(TABLES)
    assert cfg.missing_settings == sorted(GRADE_D_KEYS)


def test_commands_on_an_empty_d1_refuse_without_a_traceback(tmp_path, empty_d1, caplog):
    from cyris.entrypoints.cli import app

    args = ["--config", str(empty_d1), "--sources", str(tmp_path / "s.yaml")]
    runner = CliRunner()

    sync = runner.invoke(app, ["promote-sync", *args])
    run = runner.invoke(app, ["run", "--if-due", *args])
    doctor = runner.invoke(app, ["doctor", *args])

    assert sync.exit_code == 0, sync.output
    assert "Promotion sync not configured" in sync.output
    assert run.exit_code == 1
    assert run.exception is None or isinstance(run.exception, SystemExit)
    assert "Missing settings in D1" in caplog.text
    for text in (run.output, caplog.text):
        assert "Traceback" not in text and "AttributeError" not in text
    assert doctor.exit_code == 1
    assert "✗ settings" in doctor.output
    assert "Traceback" not in doctor.output


def test_make_config_overrides_one_value_and_keeps_the_rest():
    cfg = make_config(digest={"max_featured": 3})

    assert cfg.app.digest.max_featured == 3
    for key, value in TEST_SETTINGS.items():
        if key == "digest.max_featured":
            continue
        table, field = key.split(".", 1)
        assert getattr(getattr(cfg.app, table), field) == value, key
    assert set(TEST_SETTINGS) == set(GRADE_D_KEYS)


def test_one_setting_validates_while_its_siblings_are_required():
    assert validate_setting("digest.max_featured", 3) == 3


def test_the_example_config_builds_every_settings_table():
    cfg = load_config(ROOT / "cyris.toml.example", ROOT / "sources.example.yaml")

    assert all(getattr(cfg.app, table) is not None for table in TABLES)


def _grade_d_parameters():
    from cyris.domain import selection
    from cyris.service_layer import (
        cluster_news,
        digest_pipeline,
        filtering,
        prompts,
        scoring,
        summarize,
        vote_similarity,
    )

    pipeline = digest_pipeline.DigestPipeline
    return [
        (pipeline.__init__, "max_digest_output"),
        (pipeline.__init__, "summarize_snippet_length"),
        (pipeline.__init__, "filter_snippet_length"),
        (pipeline.__init__, "score_threshold"),
        (pipeline.__init__, "style_prompt"),
        (pipeline.process, "timezone"),
        (filtering.filter_articles, "filter_snippet_length"),
        (filtering.filter_articles, "style_prompt"),
        (summarize.summarize_articles, "snippet_length"),
        (summarize.summarize_articles, "style_prompt"),
        (cluster_news.cluster_news, "style_prompt"),
        (scoring.score_articles_batch, "snippet_length"),
        (scoring.score_in_batches, "snippet_length"),
        (vote_similarity.judge_by_votes, "max_seeds"),
        (prompts.build_filter_prompt, "snippet_length"),
        (prompts.build_summarize_prompt, "snippet_length"),
        (prompts.build_scoring_prompt, "snippet_length"),
        (prompts.build_filter_system_prompt, "style_prompt"),
        (prompts.build_summarize_system_prompt, "style_prompt"),
        (prompts.build_news_cluster_system_prompt, "style_prompt"),
        (selection.select_digest_articles, "max_items"),
    ]


@pytest.mark.parametrize(
    ("func", "name"),
    _grade_d_parameters(),
    ids=lambda v: v if isinstance(v, str) else v.__qualname__,
)
def test_no_callable_carries_a_runtime_setting_as_a_default(func, name):
    """A caller that leaves a setting out must fail, not run on an old number."""
    param = inspect.signature(func).parameters[name]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
