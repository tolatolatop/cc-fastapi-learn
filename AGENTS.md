# Repository Guidelines

## Project Structure & Module Organization

The FastAPI backend lives in `src/cc_fastapi/`: HTTP handlers are in `api/`, schemas in `schemas/`, operations in `services/`, persistence in `db/`, and orchestration in `workflows/`. Configuration is in `config/`; tests are in `tests/`.

The React/Vite console is in `frontend/`, with code under `frontend/src/`. Bootstrap tokens live in `bootstrap-theme.scss`; business layouts remain in `styles.css`. Nginx and container files stay at the frontend root. Put architecture notes in `docs/` and examples in `examples/`.

## Build, Test, and Development Commands

- `docker compose up --build`: build and run the API and console together.
- `poetry install`: install backend dependencies.
- `poetry run uvicorn cc_fastapi.main:app --reload`: run the API locally.
- `pytest -q`: run the complete backend test suite.
- `ruff check src tests`: lint Python sources and tests.
- `cd frontend && npm ci && npm run dev`: install frontend dependencies and start Vite.
- `cd frontend && npm run build`: type-check and create the production frontend bundle.
- `docker compose config --quiet`: validate Compose configuration before deployment changes.

## Coding Style & Naming Conventions

Use four-space indentation, type hints, and `snake_case` for Python; classes and Pydantic/SQLAlchemy models use `PascalCase`. Preserve the API → service → database separation. React and TypeScript use two-space indentation, single quotes, no semicolons, `PascalCase` components, and `camelCase` variables. Keep API types in `frontend/src/types.ts` and network calls in `frontend/src/api.ts`.

Use React-Bootstrap for buttons, forms, modals, offcanvas drawers, tables, and pagination. Adjust shared colors, typography, spacing, and radii in `bootstrap-theme.scss`. Reserve `styles.css` for queue, Webhook, review visualizations, and responsive business layouts; do not recreate Bootstrap primitives with page-specific CSS.

## Testing Guidelines

Tests use `pytest`; name files `test_<feature>.py` and tests `test_<behavior>()`. Cover API success, validation, filtering, and conflict paths. Concurrency-sensitive workflow or queue changes require a concurrent regression test. All new behavior needs tests. Run backend tests and the frontend production build before submitting.

## Commit & Pull Request Guidelines

History favors short, imperative subjects such as `Add review issue statistics and console`; `fix:` and `docs:` prefixes are also accepted. Keep commits focused, and commit `package-lock.json` with dependency changes. Pull requests should explain changes, link issues, list verification commands, call out schema or configuration changes, and include screenshots for console changes.

## Security & Configuration Tips

Never commit API keys, API tokens, webhook secrets, or populated `.env` files. Use environment variables documented in `README.md`, and use synthetic payloads and credentials in tests.
