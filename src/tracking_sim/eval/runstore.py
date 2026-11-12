"""The immutable run store.

Every run of a scenario is written once to ``<root>/<run id>/`` and never
changed. The run id is a hash of everything that determines the result:

* the resolved scenario (every sensor and tracker spec inlined, the seed);
* a digest of the input data (the truth recording or SBS files);
* the code: a digest of the ``tracking_sim`` source files and the versions
  of the libraries that shape results (Stone Soup, NumPy, SciPy).

So the same scenario, data and code always map to the same run, and running
it again reuses the stored result; change any of them and it is a new run.
Hashing the source rather than a git commit means two different
uncommitted edits can't share an id. The git commit and whether the tree
had uncommitted changes are still recorded in the manifest, for people.

A run directory holds ``manifest.json`` (id, created time, inputs, code,
resolved scenario, timings) plus everything ``ScenarioResult.write`` writes.
It is assembled in a temporary directory and renamed into place, so a
crashed run never leaves a half-written run behind, and its files are made
read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import tracking_sim

__all__ = ["RunRecord", "RunStore", "code_identity", "input_digest", "run_id"]

_RESULT_LIBRARIES = ("stonesoup", "numpy", "scipy")
_PACKAGE_DIR = Path(tracking_sim.__file__).resolve().parent


def _sha256_files(paths: list[Path], root: Path) -> str:
    h = hashlib.sha256()
    for path in paths:
        h.update(str(path.relative_to(root)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def input_digest(path: Path) -> str:
    """SHA-256 of a file, or of every file under a directory (names included)."""
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file())
        return _sha256_files(files, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_PACKAGE_DIR,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def code_identity() -> dict[str, Any]:
    """What code produced a run: the part that is hashed, and notes for people."""
    sources = sorted(_PACKAGE_DIR.rglob("*.py"))
    libraries: dict[str, str | None] = {}
    for name in _RESULT_LIBRARIES:
        try:
            libraries[name] = version(name)
        except PackageNotFoundError:  # pragma: no cover - always installed
            libraries[name] = None
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--", str(_PACKAGE_DIR))
    return {
        "hashed": {
            "package": tracking_sim.__version__,
            "source_sha256": _sha256_files(sources, _PACKAGE_DIR),
            "libraries": libraries,
        },
        "git_commit": commit,
        "git_uncommitted_changes": bool(status) if status is not None else None,
    }


def run_id(
    name: str, resolved: dict[str, Any], inputs: dict[str, str], code: dict[str, Any]
) -> str:
    """``<scenario name>-<12 hex digits>`` from everything that determines a result."""
    payload = {"scenario": resolved, "inputs": inputs, "code": code["hashed"]}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{name}-{digest[:12]}"


@dataclass
class RunRecord:
    """A stored run, read back from disk."""

    path: Path
    manifest: dict[str, Any]
    reused: bool = False

    @property
    def id(self) -> str:
        run: str = self.manifest["id"]
        return run

    @property
    def metrics(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.path / "metrics.json").read_text())
        return data

    @property
    def table(self) -> str:
        return (self.path / "comparison.txt").read_text().rstrip("\n")

    @property
    def summary(self) -> str:
        return (self.path / "summary.txt").read_text().rstrip("\n")

    def describe(self) -> str:
        m = self.manifest
        code = m["code"]
        dirty = " (uncommitted changes)" if code.get("git_uncommitted_changes") else ""
        commit = (code.get("git_commit") or "unknown")[:10]
        return "\n".join(
            [
                f"run {m['id']}",
                f"  scenario {m['scenario']['name']}, seed {m['scenario']['seed']}",
                f"  created {m['created']}",
                f"  truth {m['scenario']['truth_path']} (sha256 {m['inputs']['truth'][:12]})",
                f"  code tracking-sim {code['hashed']['package']}, commit {commit}{dirty}",
                f"  path {self.path}",
            ]
        )


class RunStore:
    """Runs under ``root`` (default ``runs/``), one immutable directory each."""

    def __init__(self, root: str | Path = "runs") -> None:
        self.root = Path(root)

    def plan(self, scenario: Any) -> tuple[str, dict[str, Any]]:
        """The run id ``scenario`` would get, and its manifest without results."""
        resolved = scenario.resolved()
        inputs = {"truth": input_digest(scenario.truth_path)}
        code = code_identity()
        rid = run_id(scenario.spec.name, resolved, inputs, code)
        manifest = {
            "id": rid,
            "inputs": inputs,
            "code": code,
            "scenario": resolved,
        }
        return rid, manifest

    def get(self, run: str) -> RunRecord:
        """A stored run by id or unique id prefix."""
        exact = self.root / run
        if (exact / "manifest.json").exists():
            return self._load(exact)
        matches = [r for r in self.list() if r.id.startswith(run)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"no run {run!r} in {self.root}")
        raise KeyError(f"{run!r} matches {len(matches)} runs: {[m.id for m in matches]}")

    def list(self) -> list[RunRecord]:
        """Every complete run, oldest first."""
        if not self.root.exists():
            return []
        runs = [
            self._load(d)
            for d in self.root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and (d / "manifest.json").exists()
        ]
        return sorted(runs, key=lambda r: (r.manifest["created"], r.id))

    def run(self, scenario: Any) -> RunRecord:
        """Run ``scenario`` unless an identical run is stored, and return the stored run."""
        from tracking_sim.sim.scenario import run_scenario

        rid, manifest = self.plan(scenario)
        target = self.root / rid
        if (target / "manifest.json").exists():
            record = self._load(target)
            record.reused = True
            return record

        result = run_scenario(scenario)
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{rid}-", dir=self.root))
        try:
            result.write(staging)
            (staging / "summary.txt").write_text(result.summary() + "\n")
            manifest["created"] = datetime.now(UTC).isoformat(timespec="seconds")
            manifest["timings_s"] = result.timings_s
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            _make_read_only(staging)
            try:
                os.rename(staging, target)
            except OSError:
                # Someone stored the same run meanwhile: theirs stands, ours goes.
                if not (target / "manifest.json").exists():
                    raise
                _remove(staging)
        except BaseException:
            _remove(staging)
            raise
        return self._load(target)

    def _load(self, path: Path) -> RunRecord:
        manifest = json.loads((path / "manifest.json").read_text())
        return RunRecord(path=path, manifest=manifest)


def _make_read_only(directory: Path) -> None:
    for path in directory.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def _remove(directory: Path) -> None:
    """Delete a (possibly read-only) staging directory."""
    if not directory.exists():
        return
    for path in directory.rglob("*"):
        path.chmod(stat.S_IRWXU)
    shutil.rmtree(directory)
