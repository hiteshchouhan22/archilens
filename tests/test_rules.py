"""
Tests for architecture rules: rule types, capability rules, cycles, config
validation, the base-ref baseline in `archilens diff`, `archilens check`, and
the MCP pre-edit check.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from archilens.analyzers.diff import check_rules, compute_diff, format_diff_as_markdown
from archilens.config import CIRule, ConfigError, load_config
from archilens.models import ArchEdge, ArchNode, ArchSnapshot, DiagramLevel, EdgeType, NodeType
from archilens.rules import evaluate_rules

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _snap(edges: list[tuple[str, str]], capabilities: dict[str, str] | None = None, ref: str = "head") -> ArchSnapshot:
    capabilities = capabilities or {}
    names = sorted({m for e in edges for m in e} | set(capabilities))
    nodes = [
        ArchNode(id=f"module:{m}", name=m, node_type=NodeType.MODULE, level=DiagramLevel.MODULE,
                 capability=capabilities.get(m))
        for m in names
    ]
    return ArchSnapshot(
        project_name="t", git_ref=ref, nodes=nodes,
        edges=[ArchEdge(source=f"module:{s}", target=f"module:{t}", edge_type=EdgeType.DEPENDENCY) for s, t in edges],
    )


# ---------------------------------------------------------------------------
# Rule types
# ---------------------------------------------------------------------------


class TestRuleTypes:
    def test_rule_name_no_longer_selects_the_type(self):
        rule = CIRule(name="ui-must-not-touch-db", type="forbidden-dependency", action="fail",
                      from_pattern="ui/**", to_pattern="db/**")
        violations = evaluate_rules(_snap([("ui/app", "db/repo"), ("ui/app", "core")]), [rule])
        assert [v.message for v in violations] == ["Forbidden dependency from ui/app to db/repo"]
        assert violations[0].action == "fail"

    def test_legacy_names_still_work(self):
        rules = [
            CIRule(name="no-layer-skip", from_pattern="ui/**", to_pattern="db/**"),
            CIRule(name="max-fan-out", threshold=1),
        ]
        kinds = {v.rule_type for v in evaluate_rules(_snap([("ui", "db"), ("ui", "core")]), rules)}
        assert kinds == {"forbidden-dependency", "max-fan-out"}

    def test_capability_rule(self):
        rule = CIRule(name="payments-independent-of-identity", type="forbidden-dependency", action="fail",
                      from_capability="Payments", to_capability="Identity")
        snap = _snap(
            [("billing/charge", "auth/users"), ("billing/charge", "billing/ledger"), ("orders", "auth/users")],
            capabilities={"billing/charge": "Payments", "billing/ledger": "Payments", "auth/users": "Identity",
                          "orders": "Orders"},
        )
        violations = evaluate_rules(snap, [rule])
        assert [v.message for v in violations] == [
            "Forbidden dependency from billing/charge to auth/users (Payments -> Identity)"
        ]

    def test_path_and_capability_on_one_side_must_both_match(self):
        rule = CIRule(name="r", type="forbidden-dependency", from_pattern="api/**", from_capability="Public",
                      to_pattern="db/**")
        snap = _snap([("api/v1", "db/x"), ("api/internal", "db/x")],
                     capabilities={"api/v1": "Public", "api/internal": "Internal"})
        assert [v.modules for v in evaluate_rules(snap, [rule])] == [["api/v1", "db/x"]]

    def test_no_cycles(self):
        rule = CIRule(name="acyclic", type="no-cycles", action="fail")
        snap = _snap([("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("x", "y")])
        violations = evaluate_rules(snap, [rule])
        assert [v.message for v in violations] == ["Dependency cycle between a, b, c"]
        assert violations[0].key == "acyclic|a,b,c"

    def test_fan_out_counts_distinct_modules_and_ignores_class_edges(self):
        rule = CIRule(name="fan", type="max-fan-out", threshold=2)
        snap = _snap([("hub", "a"), ("hub", "b")])
        snap.edges.append(ArchEdge(source="class:hub/x.py:X", target="class:a/y.py:Y", edge_type=EdgeType.INHERITANCE))
        snap.edges.append(ArchEdge(source="module:hub", target="module:a", edge_type=EdgeType.DEPENDENCY))  # duplicate
        assert evaluate_rules(snap, [rule]) == []
        snap.edges.append(ArchEdge(source="module:hub", target="module:c", edge_type=EdgeType.DEPENDENCY))
        assert [v.message for v in evaluate_rules(snap, [rule])] == ["Module hub has 3 dependencies (threshold: 2)"]


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestRuleConfig:
    def _load(self, tmp_path: Path, rules_yaml: str, extra: str = ""):
        (tmp_path / ".archilens.yml").write_text(f"ci:\n{extra}  rules:\n{rules_yaml}", encoding="utf-8")
        return load_config(tmp_path)

    def test_typed_rule_parses(self, tmp_path):
        cfg = self._load(tmp_path, '    - name: "no-ui-db"\n      type: "forbidden-dependency"\n'
                                   '      from: "ui/**"\n      to_capability: "Data"\n      action: "fail"\n')
        rule = cfg.ci.rules[0]
        assert (rule.kind, rule.from_pattern, rule.to_capability, rule.action) == (
            "forbidden-dependency", "ui/**", "Data", "fail")
        assert cfg.ci.fail_on == "new"

    @pytest.mark.parametrize(
        ("rules_yaml", "message"),
        [
            ('    - name: "custom-name"\n      from: "a/**"\n      to: "b/**"\n', "set 'type'"),
            ('    - name: "r"\n      type: "no-spaghetti"\n', "unknown type"),
            ('    - name: "r"\n      type: "forbidden-dependency"\n      from: "a/**"\n', "needs"),
            ('    - name: "r"\n      type: "max-fan-out"\n', "threshold"),
            ('    - name: "r"\n      type: "no-cycles"\n      action: "block"\n', "action"),
        ],
    )
    def test_rules_that_could_never_fire_are_errors(self, tmp_path, rules_yaml, message):
        with pytest.raises(ConfigError, match=message):
            self._load(tmp_path, rules_yaml)

    def test_fail_on_validated(self, tmp_path):
        with pytest.raises(ConfigError, match="fail_on"):
            self._load(tmp_path, '    - name: "c"\n      type: "no-cycles"\n', extra='  fail_on: "sometimes"\n')


# ---------------------------------------------------------------------------
# Baseline: new vs existing violations
# ---------------------------------------------------------------------------


class TestBaseline:
    RULES = [
        CIRule(name="no-ui-db", type="forbidden-dependency", action="fail", from_pattern="ui/**", to_pattern="db/**")
    ]

    def test_existing_violations_are_separated_from_new_ones(self):
        base = _snap([("ui/legacy", "db/repo")], ref="main")
        head = _snap([("ui/legacy", "db/repo"), ("ui/new", "db/repo")])
        diff = compute_diff(base, head)
        new = check_rules(diff, head, self.RULES, base=base)
        assert new == ["[FAIL] Rule 'no-ui-db': Forbidden dependency from ui/new to db/repo"]
        assert [v.modules for v in diff.existing_violations] == [["ui/legacy", "db/repo"]]

    def test_without_base_everything_is_new(self):
        head = _snap([("ui/legacy", "db/repo")])
        diff = compute_diff(_snap([]), head)
        assert len(check_rules(diff, head, self.RULES)) == 1

    def test_markdown_lists_new_and_folds_existing(self):
        base = _snap([("ui/legacy", "db/repo")], ref="main")
        head = _snap([("ui/legacy", "db/repo")])
        diff = compute_diff(base, head)
        check_rules(diff, head, self.RULES, base=base)
        md = format_diff_as_markdown(diff)
        assert "No new rule violations." in md
        assert "<details><summary>1 existing violation(s), already present on <code>main</code></summary>" in md
        # the action's violations-count output counts lines starting "- [": existing ones must not match
        assert not [line for line in md.splitlines() if line.startswith("- [")]


@needs_git
class TestDiffCommandBaseline:
    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.com", "-C", str(repo), *args],
                       check=True, capture_output=True)

    @pytest.fixture
    def repo(self, tmp_path):
        files = {
            ".archilens.yml": (
                "ci:\n  rules:\n    - name: no-ui-db\n      type: forbidden-dependency\n"
                "      from: \"app/ui/**\"\n      to: \"app/db/**\"\n      action: fail\n"
            ),
            "app/ui/legacy.py": "from app.db.repo import get\n",
            "app/db/repo.py": "def get(): pass\n",
            "app/ui/__init__.py": "",
        }
        for rel, body in files.items():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text(body, encoding="utf-8")
        self._git(tmp_path, "init", "-q")
        self._git(tmp_path, "add", ".")
        self._git(tmp_path, "commit", "-q", "-m", "base")
        self._git(tmp_path, "tag", "base")
        return tmp_path

    def _diff(self, repo: Path):
        from archilens.cli import main

        return CliRunner().invoke(main, ["diff", "--repo", str(repo), "--base", "base", "--head", "HEAD"])

    def test_pre_existing_violation_does_not_fail(self, repo):
        (repo / "README.md").write_text("unrelated change\n", encoding="utf-8")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-q", "-m", "docs")
        result = self._diff(repo)
        assert result.exit_code == 0, result.output
        assert "existing violation" in result.output

    def test_new_violation_fails_while_old_one_stays_existing(self, repo):
        # app/ui/pages is a new module that adds a second ui -> db dependency
        (repo / "app/ui/pages").mkdir()
        (repo / "app/ui/pages/home.py").write_text("from app.db.repo import get\n", encoding="utf-8")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-q", "-m", "new violation")
        result = self._diff(repo)
        assert result.exit_code == 1, result.output
        assert "- [FAIL] Rule 'no-ui-db': Forbidden dependency from app/ui/pages to app/db" in result.output
        assert "- (existing) [FAIL] Rule 'no-ui-db': Forbidden dependency from app/ui to app/db" in result.output

    def test_fail_on_all_restores_strict_mode(self, repo):
        cfg = repo / ".archilens.yml"
        cfg.write_text(cfg.read_text(encoding="utf-8").replace("ci:\n", "ci:\n  fail_on: all\n"), encoding="utf-8")
        self._git(repo, "commit", "-q", "-am", "strict")
        assert self._diff(repo).exit_code == 1


# ---------------------------------------------------------------------------
# archilens check
# ---------------------------------------------------------------------------


class TestCheckCommand:
    def _repo(self, tmp_path: Path, action: str) -> Path:
        files = {
            ".archilens.yml": (
                "ci:\n  rules:\n    - name: acyclic\n      type: no-cycles\n"
                f"      action: {action}\n"
            ),
            "app/a/x.py": "from app.b.y import f\n",
            "app/b/y.py": "from app.a.x import g\n",
            "lib/z.py": "",
        }
        for rel, body in files.items():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text(body, encoding="utf-8")
        return tmp_path

    def test_fail_rule_exits_1(self, tmp_path):
        from archilens.cli import main

        result = CliRunner().invoke(main, ["check", "--repo", str(self._repo(tmp_path, "fail"))])
        assert result.exit_code == 1
        assert "[FAIL] Rule 'acyclic': Dependency cycle between app/a, app/b" in result.output

    def test_warn_rule_exits_0_and_json_output(self, tmp_path):
        from archilens.cli import main

        result = CliRunner().invoke(main, ["check", "--repo", str(self._repo(tmp_path, "warn")), "--json-output"])
        assert result.exit_code == 0
        payload = json.loads(result.output[result.output.index("[") :])
        assert payload[0]["rule_type"] == "no-cycles"

    def test_no_rules(self, tmp_path):
        from archilens.cli import main

        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert CliRunner().invoke(main, ["check", "--repo", str(tmp_path)]).exit_code == 0


# ---------------------------------------------------------------------------
# MCP pre-edit check
# ---------------------------------------------------------------------------


class TestProposedDependency:
    @pytest.fixture(autouse=True)
    def _mcp(self):
        pytest.importorskip("mcp.server.fastmcp", reason="requires the 'mcp' extra")

    def test_capability_rule_blocks(self):
        from archilens.mcp.server import proposed_dependency_verdict

        snap = _snap([], capabilities={"billing": "Payments", "auth": "Identity"})
        rules = [CIRule(name="p", type="forbidden-dependency", action="fail",
                        from_capability="Payments", to_capability="Identity")]
        verdict = proposed_dependency_verdict(snap, rules, "billing", "auth")
        assert verdict["allowed"] is False and verdict["rule"] == "p"

    def test_new_edge_closing_a_cycle_is_reported(self):
        from archilens.mcp.server import proposed_dependency_verdict

        snap = _snap([("a", "b"), ("b", "c")])
        rules = [CIRule(name="acyclic", type="no-cycles", action="warn")]
        verdict = proposed_dependency_verdict(snap, rules, "c", "a")
        assert verdict["allowed"] is True and "closes a cycle" in verdict["reason"]
        assert proposed_dependency_verdict(snap, rules, "a", "c") is None

    def test_fan_out_threshold(self):
        from archilens.mcp.server import proposed_dependency_verdict

        snap = _snap([("hub", "a"), ("hub", "b")])
        rules = [CIRule(name="fan", type="max-fan-out", threshold=2, action="fail")]
        assert proposed_dependency_verdict(snap, rules, "hub", "a") is None  # already a dependency
        assert proposed_dependency_verdict(snap, rules, "hub", "c")["allowed"] is False

    def test_target_given_as_a_path_inside_a_module(self):
        from archilens.mcp.server import proposed_dependency_verdict

        snap = _snap([], capabilities={"ui": "UI", "db/repo": "Data"})
        rules = [CIRule(name="r", type="forbidden-dependency", action="fail", from_pattern="ui", to_pattern="db/**")]
        assert proposed_dependency_verdict(snap, rules, "ui", "db/repo/models")["allowed"] is False


def test_action_counts_violations_by_the_marker_the_report_uses():
    """github_action/action.yml counts lines starting with "- [" (new violations only, see TestBaseline)."""
    action = Path(__file__).parent.parent / "github_action" / "action.yml"
    assert "grep -c '^- \\[' " in action.read_text(encoding="utf-8")


def test_repository_config_passes_its_own_fail_rules():
    """ArchiLens's own .archilens.yml (also enforced in CI by .github/workflows/archilens.yml)."""
    from archilens.engine import analyze_repository

    repo = Path(__file__).parent.parent
    config = load_config(repo)
    assert {r.name for r in config.ci.rules if r.action == "fail"}  # the gate is not empty
    snapshot = analyze_repository(repo, config, use_ai=False)
    failing = [v.text() for v in evaluate_rules(snapshot, config.ci.rules) if v.action == "fail"]
    assert failing == []
