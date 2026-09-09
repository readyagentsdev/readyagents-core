.PHONY: test lint run-example fmt install smoke ci schema-check

install:
	python -m pip install -e ".[dev]"

test:
	python -m pytest

lint:
	ruff check src tests
	ruff format --check src tests

fmt:
	ruff check --fix src tests
	ruff format src tests

run-example:
	readyagents run examples/calc_pipeline.yaml
	readyagents run examples/approval_gate.yaml --approve gate
	readyagents run examples/composed_gate.yaml --approve gate

# Keyless example set used by CI: list_dir, eval, dry-run, resume, approval, parallel, include.
smoke:
	readyagents run examples/calc_pipeline.yaml --no-persist
	readyagents run examples/list_dir.yaml --no-persist
	readyagents eval examples/eval/pass.yaml
	readyagents run examples/connector_demo.yaml --pack examples/packs/connector_pack.py --no-persist
	readyagents run examples/approval_gate.yaml --approve gate --no-persist
	readyagents run examples/browser_approval.yaml --approve gate --no-persist
	readyagents run examples/fanout_gate.yaml --approve gate --no-persist
	readyagents run examples/include_demo.yaml --no-persist
	readyagents run examples/composed_gate.yaml --approve gate --no-persist
	readyagents run examples/multi_gate.yaml --approve first --approve second --no-persist
	readyagents run examples/support_triage.yaml --dry-run --no-persist --input message=hello
	readyagents run examples/agent_tools.yaml --dry-run --no-persist
	readyagents run examples/foreach_calc.yaml --no-persist
	readyagents run examples/json_mutate.yaml --no-persist
	@rm -rf .readyagents-smoke
	@set -e; \
	  export READYAGENTS_HOME=.readyagents-smoke; \
	  set +e; out=$$(readyagents run examples/approval_gate.yaml 2>&1); ec=$$?; set -e; \
	  printf '%s\n' "$$out"; \
	  test $$ec -eq 2; \
	  rid=$$(printf '%s\n' "$$out" | tr '\n' ' ' | sed -n 's/.*resume \([0-9a-f]\{16,\}\).*/\1/p'); \
	  test -n "$$rid"; \
	  readyagents resume $$rid --approve gate
	@rm -rf .readyagents-smoke

schema-check:
	readyagents schema --check schemas/workflow-v1.json

ci: lint schema-check test smoke
