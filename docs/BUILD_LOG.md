# Build log

## 2026-10-29 - S0 bootstrap
- Added backend package `finn` (12 empty subpackages), pytest smoke tests, ruff config and a minimal FastAPI app with `/health` so `make dev-api` works.
- Added Next.js App Router + TypeScript + Tailwind app in `web/` with a placeholder "FINN" home page; added a `typecheck` script.
- Added `config/` (portfolio with research set; empty categories and materiality stubs), `.env.example`, `.gitignore`, Makefile and a CI workflow running `make test` on pull requests.
- Decisions: backend deps are unpinned as specified; `requires-python >=3.11` with CI/local venv on 3.11. Dropped `next/font/google` from the scaffold so builds need no network.
- Open issues: `SMALLCAP_TBD` is a placeholder until S1; `config/categories.yaml` and `config/materiality.yaml` are empty stubs.
