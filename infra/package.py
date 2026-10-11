"""Stage the Lambda deployment package: recall_agent/ plus pinned, pure-Python dependencies.

Why not a layer or Docker bundling: the only third-party dependency is openpyxl (and its one
dependency), both pure Python. A staged directory is simple, works without Docker, and is
cached, so synth is fast and works offline after the first build.

Dependencies are pip-installed into infra/.build/deps only when requirements-lambda.txt
changes. The package directory infra/.build/lambda is reassembled on every synth so edits to
recall_agent/ are always picked up. boto3 is not included: the Lambda runtime provides it.

Run directly to inspect the result:  python infra/package.py
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INFRA = ROOT / "infra"
BUILD = INFRA / ".build"
COMPILED_SUFFIXES = (".so", ".pyd", ".dylib", ".dll")


def _install_deps(requirements: Path, deps: Path, stamp: Path) -> None:
    want = hashlib.sha256(requirements.read_bytes()).hexdigest()
    if deps.exists() and stamp.exists() and stamp.read_text() == want:
        return  # cached
    shutil.rmtree(deps, ignore_errors=True)
    deps.mkdir(parents=True)
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(requirements), "-t", str(deps),
           "--only-binary=:all:", "--no-compile", "--disable-pip-version-check", "-q"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        shutil.rmtree(deps, ignore_errors=True)
        raise RuntimeError("Could not install the Lambda dependencies (needs network access to PyPI "
                           f"the first time):\n{e.stderr.strip()}") from e
    bad = [p.name for p in deps.rglob("*") if p.suffix in COMPILED_SUFFIXES]
    if bad:  # a compiled wheel built for this laptop would break on Lambda
        shutil.rmtree(deps, ignore_errors=True)
        raise RuntimeError(f"Lambda dependencies must be pure Python, found compiled files: {bad[:5]}")
    stamp.write_text(want)


def stage_lambda_package(root: Path = ROOT, build_dir: Path = BUILD) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    deps = build_dir / "deps"
    _install_deps(root / "infra" / "requirements-lambda.txt", deps, build_dir / "deps.stamp")
    out = build_dir / "lambda"
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(deps, out)
    shutil.copytree(root / "recall_agent", out / "recall_agent",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return out


if __name__ == "__main__":
    path = stage_lambda_package()
    size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    print(f"staged {path}  ({size / 1e6:.1f} MB unzipped)")
    print("top level:", ", ".join(sorted(p.name for p in path.iterdir())))
