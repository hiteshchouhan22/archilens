"""
Module layout: how source files are grouped into L1 modules.

A module is a directory, named by its path. Grouping is relative to a *source
root*, the longest of these that contains the file:

- the repository root;
- Python source roots: the parent of a top-level package, such as ``src``;
- JVM roots: ``.../src/main/java`` (and ``test``, ``kotlin`` variants);
- Go module roots: directories containing ``go.mod``;
- JavaScript/TypeScript workspace packages: directories other than the
  repository root that contain ``package.json``. A workspace package is one
  module as a whole, which is how monorepos are usually reasoned about.

Below any other root, a *trunk* of directories that hold no source files and
have exactly one subdirectory is skipped (Java's ``org/acme/app``,
``src/<only-package>``, or a repository whose code all sits under one top-level
directory). The next ``depth`` directory levels then name the module. With the
default depth of 2, a repository with several top-level source directories gets
the same modules as the earlier fixed two-level grouping: ``app/orders/service.py``
belongs to ``app/orders``.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from dataclasses import dataclass, field

ROOT_MODULE = "<root>"

_JVM_ROOTS = (("src", "main", "java"), ("src", "test", "java"), ("src", "main", "kotlin"), ("src", "test", "kotlin"))


@dataclass
class ModuleLayout:
    """Maps a repository-relative POSIX path to the module that owns it."""

    depth: int = 2
    #: source root -> base directory (the root plus its trunk)
    bases: dict[str, str] = field(default_factory=lambda: {"": ""})
    #: roots whose whole subtree is a single module (workspace packages)
    package_roots: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, source_paths: Iterable[str], marker_paths: Iterable[str], depth: int = 2) -> ModuleLayout:
        """
        Args:
            source_paths: Non-test source files. They decide Python and JVM roots and trunks;
                test files would otherwise stop a trunk from collapsing.
            marker_paths: Every file in the repository, for ``package.json`` and ``go.mod``.
            depth: Directory levels below a root's base that name a module.
        """
        sources = sorted(set(source_paths))
        roots: set[str] = {""}
        package_roots: set[str] = set()

        for path in marker_paths:
            directory, name = posixpath.split(path)
            if not directory or "node_modules" in directory.split("/"):
                continue
            if name == "package.json":
                package_roots.add(directory)
            elif name == "go.mod":
                roots.add(directory)

        python_packages = {posixpath.dirname(p) for p in sources if posixpath.basename(p) == "__init__.py"}
        for package in python_packages:
            parent = posixpath.dirname(package)
            if package and parent and parent not in python_packages:
                roots.add(parent)

        for path in sources:
            parts = tuple(path.split("/")[:-1])
            for suffix in _JVM_ROOTS:
                for i in range(len(parts) - len(suffix) + 1):
                    if parts[i : i + len(suffix)] == suffix:
                        roots.add("/".join(parts[: i + len(suffix)]))

        roots |= package_roots
        layout = cls(depth=depth, bases={}, package_roots=package_roots)
        ordered = sorted(roots, key=len, reverse=True)
        owned: dict[str, list[str]] = {root: [] for root in roots}
        for path in sources:
            owned[_longest_root(path, ordered)].append(path)
        for root in roots:
            layout.bases[root] = root if root in package_roots else _trunk(root, owned[root])
        return layout

    def root_for(self, path: str) -> str:
        return _longest_root(path, sorted(self.bases, key=len, reverse=True))

    def module_for(self, path: str) -> str:
        """Module path owning *path* (a file), or ``<root>`` for top-level files."""
        root = self.root_for(path)
        if root in self.package_roots:
            return root
        base = self.bases[root]
        directory = posixpath.dirname(path)
        if base and not _is_within(directory, base):
            base = root  # e.g. a test file beside the trunk
        rel = directory[len(base) :].strip("/") if base else directory
        components = rel.split("/") if rel else []
        module = "/".join(part for part in [base, *components[: self.depth]] if part)
        return module or ROOT_MODULE


def _is_within(directory: str, base: str) -> bool:
    return directory == base or directory.startswith(base + "/")


def _longest_root(path: str, roots_longest_first: list[str]) -> str:
    for root in roots_longest_first:
        if root == "" or path.startswith(root + "/"):
            return root
    return ""


def _trunk(root: str, paths: list[str]) -> str:
    """Descend from *root* while the directory has no files of its own and exactly one subdirectory."""
    base = root
    while paths:
        prefix = f"{base}/" if base else ""
        rels = [p[len(prefix) :] for p in paths]
        if any("/" not in rel for rel in rels):
            return base  # files directly in base
        children = {rel.split("/", 1)[0] for rel in rels}
        if len(children) != 1:
            return base
        base = f"{prefix}{children.pop()}"
    return base
