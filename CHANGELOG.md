# Changelog

All notable changes to ArchiLens are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

---

## [0.2.2] — 2026-10-24

### Changed
- **Default AI model is now `claude-opus-5-5`**, replacing the deprecated `claude-sonnet-4-20250514`.
  A new `ai.effort` setting (default `"low"`) controls reasoning effort on Claude 5-generation models;
  `claude-haiku-5-5` is documented as a cheaper option for summaries.
- **Claude 5-generation models use structured outputs** instead of a forced tool call, which
  `claude-opus-5-5` and `claude-sonnet-5-5` reject. Older models keep the forced tool call. Requests to
  `claude-opus-5-5`, `claude-opus-5`, `claude-fable-5-1` and `claude-sonnet-5-5` enable server-side
  refusal fallback. Requires `anthropic>=2.3`.
- **AI is skipped with a single warning when no Anthropic credentials are available**, or the model
  doesn't exist, instead of logging two warnings per module. Credentials are checked once up front
  with the Models API, which costs no tokens.
- **Files ignored by `.gitignore` are no longer analysed.** Inside a Git work tree, files come from
  `git ls-files`. The default excludes now also cover `.venv`, `site-packages`, `.tox`, `target`,
  `vendor`, `.next`, `coverage` and `*.min.js`. `archilens init` no longer writes an `exclude` list
  that replaced these defaults.
- **Files under `test/`, `tests/`, `__tests__/`, `spec/` and `testdata/` directories are treated as
  tests** at any depth, so a top-level `tests/` package no longer appears as the largest module.
- **The viewer binds to `127.0.0.1` by default**; `--host` changes it. `--debug` (the Werkzeug
  debugger) is refused unless the host is loopback.
- **The MCP server re-analyses when the repository changes**: a new commit, uncommitted edits,
  a `.archilens.yml` change or an ArchiLens upgrade. Previously a snapshot was reused for as long as
  HEAD stayed the same, including across ArchiLens upgrades.
- The `serve-mcp` setup hint now shows `claude mcp add` instead of a non-existent config path.
- The composite action writes the report to the job summary, and no longer fails when it can't
  comment on a fork PR.

### Fixed
- **Analysis is much faster**: each file is parsed once instead of three times, and
  Tree-sitter queries are compiled once per language. On nestjs/nest (1,521 TypeScript files) the
  dependency stage went from 44 s to 0.5 s.
- Cycle detection no longer enumerates every cycle, which could take exponential time on dense graphs.
- **Output is reproducible**: `INDEX.md` no longer embeds a timestamp (so the diagram workflow no
  longer commits on every push), paths are POSIX-style on every OS, and edges and diff entries are
  sorted.
- `archilens diff` reported new classes as "New module added"; entries now name the node type.
  Removing a class is `info`; removing a module stays a `warning`.
- AI summaries containing quotes, `<`, `>`, `|` or newlines broke Mermaid and D2 syntax; labels are
  now escaped.
- The viewer inserted module names, AI summaries and error messages into the page as raw HTML;
  they are now escaped.

### Docs
- README comparison table corrected against each competitor's current docs, with the date checked,
  plus a note on current language limitations.
- New sections for the MCP server and the composite action; rule types documented.
- Landing page claims about AI and Tree-sitter corrected.

---

## [0.2.1] — 2026-09-30

### Fixed
- **Capability mapping and CI rules now match the documented glob style.** A pattern
  such as `src/orders/**` matches the module `src/orders` itself; previously it matched
  nothing, so capabilities were never assigned and `no-layer-skip` rules never fired.
  Matching is now case-sensitive and separator-normalised on every OS. The same fix
  applies to the MCP `check_dependency_allowed` tool.
- **`archilens serve-mcp` works again on a fresh install.** The `mcp` extra is pinned to
  `<2`, because mcp 2.x removed `mcp.server.fastmcp`.
- **`serve-mcp --transport sse` no longer crashes** with a `TypeError` on startup.
- **MCP `get_module_context`** returned the parent module for files in a nested module
  (e.g. `pkg` instead of `pkg/sub`); the most specific module now wins.
- **`archilens diff` prints only the markdown report on stdout**, unwrapped. Logs and
  status messages from every command now go to stderr, so `archilens diff > diff.md`
  and `archilens analyze --json-output > snapshot.json` produce clean files, and the
  stdio MCP transport is no longer at risk from log lines on stdout.
- **GitHub Action**: finds and updates its previous PR comment instead of posting a new
  one on every push; `fail-on-violations` now fails the job when a rule with action
  `fail` is violated; the `diagrams-path`, `has-changes` and `violations-count` outputs
  are set; a failed diff is reported as an error instead of being posted as a comment.
- `archilens.__version__` reported `0.1.0`; the version is now defined once, in
  `archilens/__init__.py`.

### Changed
- `serve-mcp` has a new `--host` option for the SSE transport, defaulting to
  `127.0.0.1` (the previous code intended `0.0.0.0`).
- Workflows and the composite action use Node 24 releases of the GitHub actions
  (`checkout@v7`, `setup-python@v7`, `github-script@v9`, `upload-artifact@v7`,
  `download-artifact@v8`, `codecov-action@v7`).
- CI lint job is green: Ruff lint and format pass, and mypy strict passes with no errors.
  CI installs the `viewer` and `mcp` extras so both are tested and type-checked.
- README, landing page and CONTRIBUTING document installation from PyPI.

---

## [0.2.0] — 2026-06-10

### Added
- **MCP server** (`archilens serve-mcp`): exposes architecture knowledge to AI coding agents
  (Claude Code, Cursor, Copilot) via four tools: `get_module_context`,
  `check_dependency_allowed`, `find_capability_owner`, `get_architecture_context`.
  Supports stdio and SSE transports. Snapshot cache backed by SQLite at
  `~/.archilens/snapshots.db` — avoids re-analysis on repeated agent sessions.
- **Cytoscape.js interactive graph** for L1 module architecture. Handles 500+ nodes
  with compound capability groups, LOC-proportional node sizing, weighted edges,
  click-to-drill to L2, hover tooltips, and selectable layout algorithms.
  Mermaid retained for L2 class diagrams and L3 sequence diagrams.
- New `/api/graph/l1` endpoint returning Cytoscape-format JSON with capability parent
  nodes, module nodes, and weighted dependency edges.

### Changed
- **AI module**: all three analysis functions (`infer_process_flows`,
  `generate_module_summaries`, `detect_patterns`) now use Anthropic tool use
  (`tool_choice={"type":"tool"}`) for deterministic structured output — no more
  JSON fence-stripping. Non-Anthropic providers retain the text-parsing fallback.
- `generate_module_summaries` now runs up to 5 completions in parallel via
  `ThreadPoolExecutor` — ~12x faster on large repos.
- `ArchSnapshot.version` renamed to `project_type` to correctly store
  `monolith|library|service` instead of a version number.

### Fixed
- Diff engine now emits `modified` entries for real signals: node LOC changes >20%
  between refs, and edge weight (coupling) changes >20%.
  Previously `0 modified` was always reported despite the field existing.
- Hardcoded `your-org/archilens` link in diff markdown output replaced with correct URL.

---

## [0.1.0] — 2026-05-17

### Added
- Initial release
- Core static analysis pipeline (`archilens analyze`)
- L0–L3 Mermaid diagram generation
- AI-powered module summaries and process flow inference
- `archilens diff` for architecture drift detection
- `archilens serve` for interactive web viewer
- `archilens init` for config scaffolding
- GitHub Action composite workflow
- `.archilens.yml` configuration schema

[Unreleased]: https://github.com/saurabh-oss/archilens/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/saurabh-oss/archilens/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/saurabh-oss/archilens/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/saurabh-oss/archilens/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/saurabh-oss/archilens/releases/tag/v0.1.0
