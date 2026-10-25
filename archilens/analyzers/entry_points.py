"""
Entry-point detection for process flow inference.

Used when ``analysis.entry_points`` is not configured. Entry points come from,
in order:

1. Manifests: ``[project.scripts]`` / ``[project.gui-scripts]`` /
   ``[tool.poetry.scripts]`` in ``pyproject.toml``, and ``bin`` / ``main`` in
   ``package.json``. Only targets that are discovered source files count, so a
   ``bin`` pointing at build output is ignored.
2. File-name conventions: ``main.go``, ``*Application.java``, ``app.py``,
   ``server.ts`` and the like.

Each entry point costs one AI request, so at most ``MAX_DETECTED`` are returned,
manifest entries first.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

MAX_DETECTED = 5

_SCRIPT_TABLES = {"project.scripts", "project.gui-scripts", "tool.poetry.scripts"}
_TOML_TABLE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")
_TOML_SCRIPT = re.compile(r"""^\s*["']?[\w.-]+["']?\s*=\s*["']([\w.]+)(?::[\w.]+)?["']""")

_PYTHON_NAMES = {"main.py", "app.py", "server.py", "manage.py", "wsgi.py", "asgi.py"}
_JS_STEMS = {"main", "index", "server", "app"}
_JS_EXTENSIONS = {".js", ".ts", ".mjs", ".cjs"}


def detect_entry_points(repo_path: Path, all_paths: list[str], source_paths: list[str]) -> dict[str, str]:
    """
    Detect entry points in a repository.

    Args:
        repo_path: Repository root.
        all_paths: Every candidate file (POSIX, relative), for the manifests.
        source_paths: The discovered, non-test source files (POSIX, relative).

    Returns:
        Source path -> entry type, in priority order, at most ``MAX_DETECTED``.
    """
    sources = set(source_paths)
    found: dict[str, str] = {}

    for manifest in all_paths:
        name = PurePosixPath(manifest).name
        if name == "pyproject.toml":
            for target in _pyproject_scripts(repo_path / manifest, posixpath.dirname(manifest), sources):
                found.setdefault(target, "cli")
        elif name == "package.json":
            for target, entry_type in _package_json_entries(repo_path / manifest, posixpath.dirname(manifest)):
                if target in sources:
                    found.setdefault(target, entry_type)

    package_dirs = {posixpath.dirname(p) for p in all_paths if PurePosixPath(p).name == "package.json"}
    manifest_dirs = {posixpath.dirname(p) for p in found}
    conventional = [
        (path, kind)
        for path in source_paths
        if (kind := _conventional_type(path, package_dirs, manifest_dirs)) is not None
    ]
    # Shallow files first: src/main.go before vendor-ish nested copies.
    for path, kind in sorted(conventional, key=lambda item: (item[0].count("/"), item[0])):
        found.setdefault(path, kind)

    if len(found) > MAX_DETECTED:
        logger.info(
            "Found %d entry points; using the first %d. Set analysis.entry_points in .archilens.yml to choose.",
            len(found),
            MAX_DETECTED,
        )
    return dict(list(found.items())[:MAX_DETECTED])


def _pyproject_scripts(path: Path, base: str, sources: set[str]) -> list[str]:
    """Source files that the console scripts declared in *path* point at."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    targets: list[str] = []
    table = ""
    for line in text.splitlines():
        if header := _TOML_TABLE.match(line):
            table = header.group(1).strip().replace('"', "")
            continue
        if table in _SCRIPT_TABLES and (script := _TOML_SCRIPT.match(line)):
            module = script.group(1).replace(".", "/")
            for candidate in (f"{module}.py", f"{module}/__init__.py", f"src/{module}.py", f"src/{module}/__init__.py"):
                target = posixpath.join(base, candidate) if base else candidate
                if target in sources:
                    targets.append(target)
                    break
    return targets


def _package_json_entries(path: Path, base: str) -> list[tuple[str, str]]:
    """``bin`` (CLI) and ``main`` targets declared in a ``package.json``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    entries: list[tuple[str, str]] = []
    bins = data.get("bin")
    for target in [bins] if isinstance(bins, str) else list(bins.values()) if isinstance(bins, dict) else []:
        if isinstance(target, str):
            entries.append((_join(base, target), "cli"))
    if isinstance(data.get("main"), str):
        entries.append((_join(base, data["main"]), "application_entry"))
    return entries


def _join(base: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(base, target)) if base else posixpath.normpath(target)


def _conventional_type(path: str, package_dirs: set[str], manifest_dirs: set[str]) -> str | None:
    """The entry type a file's name suggests, or None."""
    pure = PurePosixPath(path)
    name, parent = pure.name, posixpath.dirname(path)
    if name == "main.go":
        return "application_entry"
    if pure.suffix in (".java", ".kt") and pure.stem.endswith("Application"):
        return "application_entry"
    if name == "__main__.py":
        # Usually a two-line wrapper around the console script already found in the same package.
        return None if parent in manifest_dirs else "cli"
    if name in _PYTHON_NAMES:
        return "http_handler" if name in ("app.py", "wsgi.py", "asgi.py") else "application_entry"
    if pure.suffix in _JS_EXTENSIONS and pure.stem in _JS_STEMS:
        roots = package_dirs | {""}
        if parent in roots or (pure.parent.name == "src" and posixpath.dirname(parent) in roots):
            return "http_handler" if pure.stem in ("server", "app") else "application_entry"
    return None
