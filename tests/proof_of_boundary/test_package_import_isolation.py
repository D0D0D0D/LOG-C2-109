"""PB — package-relative import isolation for vendored parts.

Regression: parts vendored under ``src/`` imported their own sibling modules by
bare top-level name (``from cat2_graph_skeleton.graph_node import ...``). That
resolved only because an editable install placed ``repo/src`` on sys.path, so any
environment importing ``src.*`` without that shim raised ``ModuleNotFoundError``,
and the part could otherwise be loaded twice under two identities
(``src.cat2_graph_skeleton`` and ``cat2_graph_skeleton``).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
_SRC_PACKAGES = {p.name for p in _SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists()}


def _module_files() -> list[Path]:
    return [p for p in _SRC.rglob("*.py") if "egg-info" not in str(p)]


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_no_absolute_import_of_src_package(path: Path) -> None:
    """No src/ module may import a vendored src/ package by bare top-level name."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".", 1)[0]
            assert root not in _SRC_PACKAGES, (
                f"{path.relative_to(_REPO_ROOT)}: 'from {node.module} import ...' imports vendored "
                f"package '{root}' as a top-level module. Use a relative import or the 'src.' prefix."
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root not in _SRC_PACKAGES, (
                    f"{path.relative_to(_REPO_ROOT)}: 'import {alias.name}' targets vendored package "
                    f"'{root}'. Use the 'src.' prefix."
                )


def test_graph_imports_without_editable_src_shim() -> None:
    """The graph module imports with only the repo root on sys.path."""

    code = (
        "import sys;"
        f"sys.path=[p for p in sys.path if p != {str(_SRC)!r}];"
        f"sys.path.insert(0, {str(_REPO_ROOT)!r});"
        "import src.graph.graph;"
        "print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"import failed without the src shim:\n{proc.stderr}"
    assert "ok" in proc.stdout
