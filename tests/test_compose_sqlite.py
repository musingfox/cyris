"""A docker compose install keeps its SQLite file in a named volume and picks the backend itself.

ADR-0025: the database lives in a named volume both services mount, and nothing on the host
reaches it, because a macOS host process and a container on one bind-mounted SQLite file
corrupt it. The compose environment, not `cyris.toml`, picks sqlite when the file names no
backend, and a file that names one still wins.
"""

import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest
import yaml

from cyris.bootstrap import load_effective_config
from cyris.config import StoreConfig

pytestmark = [pytest.mark.integration, pytest.mark.guard]

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
SERVICES = ("cyris", "ui")
STATE_DIR = PurePosixPath("/app/state")
# The binary each role execs, which the entrypoint's `exec` would otherwise start for real.
ROLE_BINARY = {"cron": "supercronic", "ui": "cyris"}


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def _volume(entry: str | dict) -> tuple[str, str, bool]:
    """(source, target, is_a_host_path) of one `volumes:` entry, short or long form."""
    if isinstance(entry, dict):
        source, target = entry.get("source", ""), entry["target"]
        return source, target, entry.get("type") == "bind"
    source, target = entry.split(":")[:2]
    return source, target, source.startswith((".", "/", "~"))


def _named_volumes(service: str) -> dict[str, str]:
    """The named volumes a service mounts, by mount path."""
    return {
        target: source
        for source, target, host_path in map(_volume, _compose()["services"][service]["volumes"])
        if not host_path
    }


@pytest.mark.parametrize("service", SERVICES)
def test_each_service_picks_sqlite_unless_the_environment_names_another(service: str) -> None:
    environment = _compose()["services"][service]["environment"]

    assert "CYRIS_STORE_BACKEND=${CYRIS_STORE_BACKEND:-sqlite}" in environment


def test_both_services_mount_one_named_volume_at_the_directory_of_the_default_file() -> None:
    mounts = {service: _named_volumes(service).get(str(STATE_DIR)) for service in SERVICES}

    [name] = set(mounts.values())
    assert name is not None, mounts
    # Declared at the top level, with no driver option that would put it on a host path.
    assert name in _compose()["volumes"]
    assert not (_compose()["volumes"][name] or {}).get("driver_opts")


def test_the_default_sqlite_path_is_in_that_directory_inside_the_container() -> None:
    """The image's working directory is /app, and the path is relative to it."""
    workdir = re.search(r"^WORKDIR (\S+)$", (ROOT / "Dockerfile").read_text(), re.M)[1]
    default = PurePosixPath(StoreConfig.model_fields["sqlite_path"].default)

    assert not default.is_absolute()
    assert (PurePosixPath(workdir) / default).parent == STATE_DIR


@pytest.mark.parametrize("service", SERVICES)
def test_nothing_on_the_host_is_mounted_over_the_state_directory_or_its_file(
    service: str,
) -> None:
    for entry in _compose()["services"][service]["volumes"]:
        source, target, host_path = _volume(entry)
        if not host_path:
            continue
        mount = PurePosixPath(target)
        # The directory, a parent of it (`.:/app` would carry it), or anything inside it.
        assert mount != STATE_DIR and STATE_DIR not in mount.parents, entry
        assert mount not in STATE_DIR.parents, entry


def test_the_ui_vault_is_writable_because_the_wizards_run_writes_html_there() -> None:
    entries = _compose()["services"]["ui"]["volumes"]

    # A bare `source:target` has no `:ro`; the long form would say `read_only`.
    assert "./agent-vault:/app/agent-vault" in entries


def _service_environment(service: str, host_env: dict[str, str]) -> dict[str, str]:
    """The environment compose hands a service's container: its `environment:` list with
    `${NAME:-default}` filled from `host_env`. (`env_file` never beats `environment:`.)"""
    pair = re.compile(r"\$\{(\w+):-([^}]*)\}")
    env = {}
    for line in _compose()["services"][service]["environment"]:
        name, _, value = line.partition("=")
        env[name] = pair.sub(lambda m: host_env.get(m[1]) or m[2], value)
    return env


def _environment_the_process_sees(tmp_path: Path, service: str, host_env: dict[str, str]) -> dict:
    """What the service's process has after the real entrypoint ran its role.

    The role's binary is a stub that records its environment: the `ui` role exports
    CYRIS_STORE_BACKEND's default, and the `cron` role exports nothing.
    """
    env = _service_environment(service, host_env)
    stubs = tmp_path / "bin"
    stubs.mkdir(parents=True)
    recorded = tmp_path / "recorded.env"
    for binary in ROLE_BINARY.values():
        stub = stubs / binary
        stub.write_text(f"#!/bin/sh\nenv > '{recorded}'\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    run_env = {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", **env}
    subprocess.run(["sh", str(ENTRYPOINT)], env=run_env, cwd=tmp_path, check=True)
    return dict(line.split("=", 1) for line in recorded.read_text().splitlines() if "=" in line)


def _backend_resolved(tmp_path: Path, service: str, toml: str, host_env=None) -> str:
    process_env = _environment_the_process_sees(tmp_path, service, host_env or {})
    install = tmp_path / "install"
    install.mkdir()
    (install / "cyris.toml").write_text(toml, encoding="utf-8")
    (install / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    # The container's working directory is where a relative `sqlite_path` lands.
    cwd = Path.cwd()
    os.chdir(install)
    try:
        with mock.patch.dict(os.environ, process_env, clear=True):
            cfg = load_effective_config(install / "cyris.toml", install / "sources.yaml")
    finally:
        os.chdir(cwd)
    return cfg.app.store.backend


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize(
    ("toml", "backend"),
    [
        ('[store]\nbackend = "json"\n', "json"),
        ('[store]\nbackend = "sqlite"\n', "sqlite"),
        ("", "sqlite"),
        ("[store]\n", "sqlite"),
    ],
    ids=["names-json", "names-sqlite", "names-none", "names-no-backend"],
)
def test_each_service_resolves_the_backend_the_file_names_else_sqlite(
    tmp_path: Path, service: str, toml: str, backend: str
) -> None:
    assert _backend_resolved(tmp_path, service, toml) == backend


@pytest.mark.parametrize("service", SERVICES)
def test_a_backend_in_the_hosts_environment_beats_the_default_but_not_the_file(
    tmp_path: Path, service: str
) -> None:
    host_env = {"CYRIS_STORE_BACKEND": "json"}

    named_in_file = '[store]\nbackend = "sqlite"\n'

    assert _backend_resolved(tmp_path / "a", service, "", host_env) == "json"
    assert _backend_resolved(tmp_path / "b", service, named_in_file, host_env) == "sqlite"


def test_the_probes_config_names_no_backend_so_the_compose_environment_picks_it() -> None:
    script = (ROOT / "scripts" / "compose_probe.sh").read_text(encoding="utf-8")
    [toml] = re.findall(r"cat >cyris\.toml <<'EOF'\n(.*?)\nEOF\n", script, re.S)

    assert "backend" not in toml
    assert 'provider = "none"' in toml


def test_the_probe_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / "compose_probe.sh")], check=True)
