set shell := ['bash', '-cu']

default: check

# --- Core quality gates ---

lint:
    uv run ruff check packages/

fmt:
    uv run ruff format packages/

fmt-check:
    uv run ruff format --check packages/

typecheck:
    uv run pyright

test:
    uv run pytest

# --- Composites ---

check: lint fmt-check typecheck

fix:
    uv run ruff format packages/
    uv run ruff check --fix packages/

all: fix typecheck test

# --- Targeted variants ---

lint-path path='packages/':
    uv run ruff check {{path}}

typecheck-path path='packages/':
    uv run pyright {{path}}

test-path path='':
    uv run pytest {{path}}

# --- Per-package test shortcuts ---

test-core:
    uv run pytest packages/voxhub-core/tests

test-schema:
    uv run pytest packages/voxhub-schema/tests

test-client:
    uv run pytest packages/voxhub-client/tests

# --- Test variants ---

test-fast:
    uv run pytest -m 'not slow and not e2e'

test-e2e:
    uv run pytest -m e2e

test-cov:
    uv run pytest --cov=packages --cov-report=term-missing

test-bench:
    uv run pytest --benchmark-only

# --- Housekeeping ---

sync:
    uv sync --group dev

clean:
    find . -type d -name __pycache__ -prune -exec rm -rf {} +
    rm -rf .pytest_cache .ruff_cache .coverage
