.PHONY: test lint typecheck run-example fmt install smoke ci schema-check

install:
	python -m pip install -e ".[dev]"

test:
	python -m pytest -n auto

lint:
	ruff check src tests
	ruff format --check src tests

typecheck:
	python scripts/typecheck.py

fmt:
	ruff check --fix src tests
	ruff format src tests

run-example:
	readyagents run examples/calc_pipeline.yaml
	readyagents run examples/approval_gate.yaml --approve gate
	readyagents run examples/composed_gate.yaml --approve gate

# Keyless example set used by CI. Python runner so Windows does not need a POSIX shell.
smoke:
	python scripts/smoke.py

schema-check:
	readyagents schema --check schemas/workflow-v1.json

ci: lint typecheck schema-check test smoke
