"""Structural tests enforcing the dependency DAG.

- Local modules in ``voxhub_core/`` must never import from
  ``voxhub_core.server``.
- ``voxhub_client`` must never import from ``voxhub_core``.
- ``voxhub_core`` must never import from ``voxhub_client``.
"""

import ast
from pathlib import Path

_CORE_SRC = Path(__file__).resolve().parents[2] / 'voxhub-core' / 'src' / 'voxhub_core'
_CLIENT_SRC = (
    Path(__file__).resolve().parents[2] / 'voxhub-client' / 'src' / 'voxhub_client'
)


def _collect_imports(source_file: Path) -> list[str]:
    """Extract all imported module names from a Python file."""
    tree = ast.parse(source_file.read_text(), filename=str(source_file))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def _python_files(directory: Path, *, exclude: str | None = None) -> list[Path]:
    """Recursively collect .py files, optionally excluding a subdirectory."""
    result = []
    for p in directory.rglob('*.py'):
        if exclude and f'/{exclude}/' in str(p):
            continue
        result.append(p)
    return result


class TestHardWall:
    """Local modules must not import from the server subpackage."""

    def test_local_modules_do_not_import_server(self):
        violations = []
        for py_file in _python_files(_CORE_SRC, exclude='server'):
            for imp in _collect_imports(py_file):
                if 'voxhub_core.server' in imp:
                    rel = py_file.relative_to(_CORE_SRC)
                    violations.append(f'{rel} imports {imp}')
        assert violations == [], 'Hard wall violated:\n' + '\n'.join(violations)


class TestDependencyDag:
    """Package-level dependency constraints."""

    def test_client_does_not_import_core(self):
        if not _CLIENT_SRC.exists():
            return
        violations = []
        for py_file in _python_files(_CLIENT_SRC):
            for imp in _collect_imports(py_file):
                if 'voxhub_core' in imp:
                    rel = py_file.relative_to(_CLIENT_SRC)
                    violations.append(f'{rel} imports {imp}')
        assert violations == [], 'Client→Core dependency violated:\n' + '\n'.join(
            violations
        )

    def test_core_does_not_import_client(self):
        violations = []
        for py_file in _python_files(_CORE_SRC):
            for imp in _collect_imports(py_file):
                if 'voxhub_client' in imp:
                    rel = py_file.relative_to(_CORE_SRC)
                    violations.append(f'{rel} imports {imp}')
        assert violations == [], 'Core→Client dependency violated:\n' + '\n'.join(
            violations
        )
