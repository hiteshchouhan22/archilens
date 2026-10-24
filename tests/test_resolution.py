"""
Golden-fixture tests for per-language import resolution and module layout.

Each fixture is a small repository in the shape real projects use (src layout,
Maven, Go modules, npm workspaces, tsconfig aliases). The assertions are the
module-level dependency edges a reader of the code would draw by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archilens.analyzers.layout import ModuleLayout
from archilens.config import ArchiLensConfig
from archilens.engine import analyze_repository


def _repo(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def _edges(repo: Path, **analysis: object) -> set[tuple[str, str]]:
    config = ArchiLensConfig()
    for key, value in analysis.items():
        setattr(config.analysis, key, value)
    snap = analyze_repository(repo, config, use_ai=False)
    return {
        (e.source.removeprefix("module:"), e.target.removeprefix("module:"))
        for e in snap.edges
        if e.edge_type.value == "dependency"
    }


def _modules(repo: Path) -> list[str]:
    snap = analyze_repository(repo, ArchiLensConfig(), use_ai=False)
    return sorted(n.id.removeprefix("module:") for n in snap.get_module_nodes())


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


class TestPython:
    def test_flat_layout_absolute_imports(self, tmp_path):
        repo = _repo(tmp_path, {
            "app/orders/service.py": "from app.payments.gateway import charge\nimport app.inventory.stock as s\n",
            "app/payments/gateway.py": "def charge(): pass\n",
            "app/inventory/stock.py": "x = 1\n",
        })
        assert _edges(repo) == {("app/orders", "app/payments"), ("app/orders", "app/inventory")}

    def test_src_layout(self, tmp_path):
        repo = _repo(tmp_path, {
            "src/shop/__init__.py": "",
            "src/shop/orders/__init__.py": "",
            "src/shop/orders/service.py": "from shop.payments.gateway import charge\n",
            "src/shop/payments/__init__.py": "",
            "src/shop/payments/gateway.py": "def charge(): pass\n",
            "examples/demo.py": "import shop.orders.service\n",
        })
        # src/shop/__init__.py is a file of its own, so the package itself is a module too
        assert _modules(repo) == ["examples", "src/shop", "src/shop/orders", "src/shop/payments"]
        assert _edges(repo) == {("src/shop/orders", "src/shop/payments"), ("examples", "src/shop/orders")}

    def test_relative_imports_resolve_within_and_across_packages(self, tmp_path):
        repo = _repo(tmp_path, {
            "app/__init__.py": "",
            "app/orders/__init__.py": "",
            "app/orders/service.py": "from .models import Order\nfrom ..payments.gateway import charge\n",
            "app/orders/models.py": "class Order: pass\n",
            "app/payments/__init__.py": "",
            "app/payments/gateway.py": "def charge(): pass\n",
            "app/models/base.py": "class Base: pass\n",  # same leaf name as the relative import
        })
        # ".models" is app/orders/models.py, not the unrelated app/models package
        assert _edges(repo) == {("app/orders", "app/payments")}

    def test_third_party_and_stdlib_imports_are_ignored(self, tmp_path):
        repo = _repo(tmp_path, {
            "app/a/x.py": "import os\nimport requests\nfrom pydantic import BaseModel\n",
            "app/b/y.py": "x = 1\n",
        })
        assert _edges(repo) == set()

    def test_example_package_does_not_shadow_third_party_name(self, tmp_path):
        # docs_src/opentelemetry is an example, not the opentelemetry library
        repo = _repo(tmp_path, {
            "lib/telemetry/tracing.py": "from opentelemetry import trace\n",
            "lib/core/app.py": "x = 1\n",
            "docs_src/opentelemetry/__init__.py": "",
            "docs_src/opentelemetry/tutorial.py": "import lib.core.app\n",
        })
        assert _edges(repo) == {("docs_src/opentelemetry", "lib/core")}


# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------


class TestJavaScriptTypeScript:
    def test_relative_imports_with_extension_and_index_probing(self, tmp_path):
        repo = _repo(tmp_path, {
            "src/orders/service.ts": (
                "import { charge } from '../payments/gateway';\n"
                "import { Base } from '../core';\n"            # directory -> core/index.ts
                "import { Money } from '../shared/money.js';\n"  # ESM .js specifier -> money.ts
            ),
            "src/payments/gateway.ts": "export function charge() {}\n",
            "src/core/index.ts": "export class Base {}\n",
            "src/shared/money.ts": "export class Money {}\n",
        })
        assert _edges(repo) == {("src/orders", "src/payments"), ("src/orders", "src/core"), ("src/orders", "src/shared")}

    def test_commonjs_require_and_reexports(self, tmp_path):
        repo = _repo(tmp_path, {
            "lib/app.js": "const router = require('./router/index.js');\n",
            "lib/router/index.js": "module.exports = {};\n",
            "lib/public/api.ts": "export * from '../internal/impl';\n",
            "lib/internal/impl.ts": "export const x = 1;\n",
        })
        assert _edges(repo) == {("lib", "lib/router"), ("lib/public", "lib/internal")}

    def test_tsconfig_paths_alias_with_comments(self, tmp_path):
        repo = _repo(tmp_path, {
            "tsconfig.json": (
                "{\n  // aliases\n  \"compilerOptions\": {\n    \"baseUrl\": \".\",\n"
                "    \"paths\": { \"@app/*\": [\"src/app/*\"], },\n  },\n}\n"
            ),
            "src/app/orders/service.ts": "import { charge } from '@app/payments/gateway';\n",
            "src/app/payments/gateway.ts": "export function charge() {}\n",
        })
        assert _edges(repo) == {("src/app/orders", "src/app/payments")}

    def test_nested_tsconfig_extends_root_base_config(self, tmp_path):
        # paths declared in the root base config are relative to the root, not to apps/web
        repo = _repo(tmp_path, {
            "tsconfig.base.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@libs/*": ["libs/*"]}}}',
            "apps/web/tsconfig.json": '{"extends": "../../tsconfig.base.json"}',
            "apps/web/main.ts": "import { ui } from '@libs/ui/button';\n",
            "libs/ui/button.ts": "export const ui = 1;\n",
        })
        assert _edges(repo) == {("apps/web", "libs/ui")}

    def test_workspace_packages_are_modules_and_resolve_by_name(self, tmp_path):
        repo = _repo(tmp_path, {
            "package.json": '{"name": "root", "private": true, "workspaces": ["packages/*"]}',
            "packages/common/package.json": '{"name": "@acme/common", "main": "index.js"}',
            "packages/common/index.ts": "export * from './decorators';\n",
            "packages/common/decorators/inject.ts": "export const Inject = 1;\n",
            "packages/core/package.json": '{"name": "@acme/core"}',
            "packages/core/injector/container.ts": (
                "import { Inject } from '@acme/common';\n"
                "import { x } from '@acme/common/decorators/inject';\n"
                "import { y } from 'lodash';\n"
            ),
        })
        assert _modules(repo) == ["packages/common", "packages/core"]
        assert _edges(repo) == {("packages/core", "packages/common")}


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------


class TestJava:
    def test_maven_layout_packages_and_static_and_wildcard_imports(self, tmp_path):
        base = "src/main/java/com/acme/shop"
        repo = _repo(tmp_path, {
            f"{base}/ShopApplication.java": "package com.acme.shop;\nimport com.acme.shop.orders.OrderService;\npublic class ShopApplication {}\n",
            f"{base}/orders/OrderService.java": (
                "package com.acme.shop.orders;\n"
                "import com.acme.shop.payments.*;\n"
                "import static com.acme.shop.util.Strings.join;\n"
                "import java.util.List;\n"
                "public class OrderService {}\n"
            ),
            f"{base}/payments/Gateway.java": "package com.acme.shop.payments;\npublic class Gateway {}\n",
            f"{base}/util/Strings.java": "package com.acme.shop.util;\npublic class Strings {}\n",
            "src/test/java/com/acme/shop/orders/OrderServiceTest.java": "package com.acme.shop.orders;\nclass OrderServiceTest {}\n",
        })
        # the reverse-domain trunk com/acme/shop is skipped when naming modules
        assert _modules(repo) == [base, f"{base}/orders", f"{base}/payments", f"{base}/util"]
        assert _edges(repo) == {
            (base, f"{base}/orders"),
            (f"{base}/orders", f"{base}/payments"),
            (f"{base}/orders", f"{base}/util"),
        }


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


class TestGo:
    def test_imports_under_the_module_path(self, tmp_path):
        repo = _repo(tmp_path, {
            "go.mod": "module github.com/acme/shop\n\ngo 1.22\n",
            "main.go": 'package main\nimport "github.com/acme/shop/internal/orders"\nfunc main() { orders.Run() }\n',
            "internal/orders/orders.go": (
                'package orders\nimport (\n  "fmt"\n  "github.com/acme/shop/internal/payments"\n)\n'
                "func Run() { fmt.Println(payments.Charge()) }\n"
            ),
            "internal/payments/payments.go": "package payments\nfunc Charge() string { return \"ok\" }\n",
            "internal/payments/payments_test.go": "package payments\n",
        })
        assert _edges(repo) == {("<root>", "internal/orders"), ("internal/orders", "internal/payments")}


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


class TestModuleLayout:
    def test_flat_repository_keeps_two_level_modules(self):
        layout = ModuleLayout.build(["app/orders/service.py", "app/payments/x.py", "lib/util.py"], [])
        assert layout.module_for("app/orders/service.py") == "app/orders"
        assert layout.module_for("lib/util.py") == "lib"
        assert layout.module_for("setup.py") == "<root>"

    def test_module_depth_is_configurable(self, tmp_path):
        repo = _repo(tmp_path, {
            "app/orders/api/views.py": "from app.orders.domain.model import Order\n",
            "app/orders/domain/model.py": "class Order: pass\n",
            "lib/billing/x.py": "",
        })
        assert _edges(repo) == set()  # depth 2: both files are in app/orders
        assert _edges(repo, module_depth=3) == {("app/orders/api", "app/orders/domain")}

    def test_single_top_level_directory_is_a_trunk(self, tmp_path):
        # everything lives under app/, so app/ itself carries no information
        repo = _repo(tmp_path, {
            "app/orders/api/views.py": "from app.orders.domain.model import Order\n",
            "app/orders/domain/model.py": "class Order: pass\n",
            "app/billing/x.py": "",
        })
        assert _edges(repo) == {("app/orders/api", "app/orders/domain")}
        assert _edges(repo, module_depth=1) == set()

    def test_test_files_do_not_block_the_trunk(self):
        layout = ModuleLayout.build(["src/pkg/a/x.py", "src/pkg/b/y.py"], [])
        assert layout.module_for("src/pkg/a/x.py") == "src/pkg/a"
        assert layout.module_for("tests/test_x.py") == "tests"

    @pytest.mark.parametrize("bad", [0, -1, "two", True])
    def test_invalid_module_depth_is_rejected(self, tmp_path, bad):
        from archilens.config import ConfigError, load_config

        (tmp_path / ".archilens.yml").write_text(f"analysis:\n  module_depth: {bad!r}\n".replace("'", '"'), encoding="utf-8")
        with pytest.raises(ConfigError, match="module_depth"):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# MCP: file -> module uses the same layout
# ---------------------------------------------------------------------------


class TestMcpModuleLookup:
    def test_longest_prefix_module_wins(self):
        from archilens.mcp.server import _module_path_for

        modules = ["<root>", "src/main/java/com/acme/shop", "src/main/java/com/acme/shop/orders", "packages/core"]
        assert _module_path_for("src/main/java/com/acme/shop/orders/OrderService.java", modules) == modules[2]
        assert _module_path_for("src/main/java/com/acme/shop/App.java", modules) == modules[1]
        assert _module_path_for("packages/core/injector/deep/container.ts", modules) == "packages/core"
        assert _module_path_for("main.go", modules) == "<root>"
        assert _module_path_for("unknown/dir/file.py", modules) is None
