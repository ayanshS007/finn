.PHONY: setup test test-backend test-web dev-web dev-api migrate verify-db

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

# Apply supabase/migrations/*.sql in order, then check the tables and the anon role's rights.
migrate:
	@test -n "$$SUPABASE_DB_URL" || { echo "SUPABASE_DB_URL is not set"; exit 1; }
	@for f in supabase/migrations/*.sql; do \
		echo "applying $$f"; psql "$$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -q -f "$$f" || exit 1; \
	done
	$(MAKE) verify-db

verify-db:
	@test -n "$$SUPABASE_DB_URL" || { echo "SUPABASE_DB_URL is not set"; exit 1; }
	psql "$$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -q -f scripts/verify_db.sql
