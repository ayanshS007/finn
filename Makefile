.PHONY: setup test test-backend test-web dev-web dev-api

BACKEND_PY := backend/.venv/bin/python

setup:
	cd backend && uv venv --allow-existing --python 3.11 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	cd web && npm ci

test: test-backend test-web

test-backend:
	cd backend && .venv/bin/python -m pytest
	cd backend && .venv/bin/python -m ruff check .

test-web:
	cd web && npm run lint
	cd web && npm run typecheck
	cd web && npm run build

dev-web:
	cd web && npm run dev

dev-api:
	cd backend && .venv/bin/python -m uvicorn finn.api.main:app --reload --port 8000
