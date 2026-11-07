.PHONY: install check lint test rules generate drill sweep image

install:
	pip install -e ".[dev]"

lint:
	ruff check .
	ruff format --check .
	mypy
	ifp generate --check

rules:
	promtool check rules slo/rules.yaml
	promtool test rules slo/tests.yaml

test:
	pytest --cov --cov-report=term-missing

check: lint rules test

generate:
	ifp generate

drill:
	ifp drill --report docs/canary-drills.md

# Needs a model server on :8080 (ifp serve).
sweep:
	ifp sweep --url http://127.0.0.1:8080 --report docs/capacity.md

image:
	docker build -t ifp:dev .
