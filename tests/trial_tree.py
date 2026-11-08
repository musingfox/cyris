"""A copy of the repo files a trial script reads, with one trial rendered in it.

`scripts/reset_trial.py` and `scripts/deploy_trial.py` act on a trial's rendered
configs, which are gitignored build artifacts; a test renders its own into a
temporary tree rather than into the checkout.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SLUG = "alice"
ACCT = "0123456789abcdef0123456789abcdef"
DIGEST = "sha256:" + "a" * 64
KV = "fedcba9876543210fedcba9876543210"
D1 = "11111111-2222-4333-8444-555555555555"
TEMPLATES = [
    "wrangler.toml",
    "workers/promote/wrangler.toml",
    "workers/rss/wrangler.toml",
    "workers/newsletter/wrangler.toml",
]


def make_tree(tmp_path: Path, *scripts: str) -> Path:
    tree = tmp_path / "tree"
    for rel in [
        "scripts/provision_trial.py",
        "scripts/derive-wrangler-config.sh",
        "settings.example.toml",
        *TEMPLATES,
        *(f"scripts/{s}.py" for s in scripts),
    ]:
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tree / rel)
    (tree / "scripts/derive-wrangler-config.sh").chmod(0o755)
    render(tree, DIGEST)
    (tree / f".env.trial-{SLUG}-wizard").write_text(
        f"DOMAIN=example.com\nACCOUNT_ID={ACCT}\nTRIAL_KV_ID={KV}\nTRIAL_D1_ID={D1}\n"
        f"IMAGE_TAG=image/1111111\nIMAGE_DIGEST={DIGEST}\n",
        encoding="utf-8",
    )
    return tree


def render(tree: Path, digest: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(tree / "scripts/provision_trial.py"),
            "render",
            "--slug",
            SLUG,
            "--domain",
            "example.com",
            "--account-id",
            ACCT,
            "--image-digest",
            digest,
            "--kv-id",
            KV,
            "--d1-id",
            D1,
        ],
        check=True,
        capture_output=True,
        cwd=tree,
    )


def load(tree: Path, name: str, monkeypatch) -> ModuleType:
    """Import `scripts/<name>.py` from the tree, with the tree's provision_trial beside it."""
    for mod in ("provision_trial", name):
        spec = importlib.util.spec_from_file_location(mod, tree / f"scripts/{mod}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, mod, module)
        spec.loader.exec_module(module)
    return sys.modules[name]
