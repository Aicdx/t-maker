# Trade Confirmation Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist manual AI point confirmations to PostgreSQL and show same-day 100-share T spread statistics in the dashboard.

**Architecture:** Add domain models for trade confirmations and stats, extend `PostgresRepository` with save/list/delete methods and schema, expose REST endpoints from `tmaker.api.app`, then wire the existing decision-panel buttons to the API and render a compact stats panel. Pairing stays in a pure backend helper so it is easy to test.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, psycopg, pytest, React 19, TypeScript, Vite.

---

## File Map

- Modify `backend/src/tmaker/domain/models.py`: add confirmation/stat Pydantic models.
- Modify `backend/src/tmaker/storage/postgres.py`: add table schema and repository methods.
- Modify `backend/src/tmaker/api/app.py`: add POST/GET/DELETE trade confirmation routes.
- Modify `backend/tests/test_postgres_storage.py`: schema and repository tests.
- Modify `backend/tests/test_api.py`: API tests with fake repository.
- Create `frontend/src/tradeStats.ts`: frontend types and formatting helpers.
- Create `frontend/tests/tradeStats.test.ts`: pure frontend helper tests.
- Modify `frontend/src/App.tsx`: fetch stats, submit selected point confirmations, render stats panel.
- Modify `frontend/src/App.css`: stats panel and button state styling.

## Tasks

### Task 1: Backend Models And Pairing

**Files:**
- Modify: `backend/src/tmaker/domain/models.py`
- Test: `backend/tests/test_trade_confirmations.py`

- [ ] Add failing tests for save payload validation and same-day pairing:
  - buy then sell produces positive PnL.
  - sell then lower buy produces positive PnL.
  - different symbols do not pair.
  - unpaired confirmations remain visible.
- [ ] Implement `TradeConfirmation`, `TradeConfirmationCreate`, `TradeConfirmationPair`, `TradeConfirmationSummary`, and `TradeConfirmationStats`.
- [ ] Implement a pure `build_trade_confirmation_stats(confirmations, trade_date)` helper in a focused module if `models.py` would become too large.
- [ ] Run `python -m pytest tests/test_trade_confirmations.py -q`.
- [ ] Commit as `feat: add trade confirmation stats model`.

### Task 2: PostgreSQL Persistence

**Files:**
- Modify: `backend/src/tmaker/storage/postgres.py`
- Test: `backend/tests/test_postgres_storage.py`

- [ ] Add failing tests that `SCHEMA_SQL` contains `t_trade_confirmations`.
- [ ] Add fake-connection tests for `save_trade_confirmation`, `list_trade_confirmations`, and `delete_trade_confirmation`.
- [ ] Extend `SCHEMA_SQL` with the new table, constraints, and index.
- [ ] Implement repository methods using the existing connection/cursor pattern.
- [ ] Run `python -m pytest tests/test_postgres_storage.py tests/test_trade_confirmations.py -q`.
- [ ] Commit as `feat: persist trade confirmations`.

### Task 3: API Endpoints

**Files:**
- Modify: `backend/src/tmaker/api/app.py`
- Test: `backend/tests/test_api.py`

- [ ] Extend `FakeRepository` in API tests with confirmation storage methods.
- [ ] Add failing tests for:
  - `POST /api/trade-confirmations` saves a selected point.
  - `GET /api/trade-confirmations/stats` defaults to today.
  - `GET /api/trade-confirmations/stats?date=YYYY-MM-DD` honors the date.
  - `DELETE /api/trade-confirmations/{id}` deletes or returns 404.
- [ ] Add routes using repository methods and the pure stats helper.
- [ ] Run `python -m pytest tests/test_api.py tests/test_postgres_storage.py tests/test_trade_confirmations.py -q`.
- [ ] Commit as `feat: expose trade confirmation api`.

### Task 4: Frontend Helpers

**Files:**
- Create: `frontend/src/tradeStats.ts`
- Create: `frontend/tests/tradeStats.test.ts`

- [ ] Add failing tests for PnL formatting, action labels, and selected point request shaping.
- [ ] Implement TypeScript types and helper functions.
- [ ] Run `pnpm build` from `frontend` to verify TypeScript and Vite bundling.
- [ ] Commit as `feat: add trade stats frontend helpers`.

### Task 5: Frontend UI Wiring

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.css`
- Test: existing frontend TypeScript build plus helper tests.

- [ ] Wire "已低吸" and "已高抛" to POST the selected `ReplayPoint`.
- [ ] Disable buttons when no point is selected or while saving.
- [ ] Fetch stats on app load and after save/delete.
- [ ] Render "做T统计" summary metrics, paired rows, and unpaired rows.
- [ ] Add delete buttons for unpaired/raw records where the API provides IDs.
- [ ] Run frontend tests and `pnpm build`.
- [ ] Commit as `feat: add trade stats dashboard`.

### Task 6: Full Verification

**Files:**
- No source changes unless verification exposes a bug.

- [ ] Run backend targeted tests:
  `D:\it\t-maker\backend\.venv\Scripts\python.exe -m pytest tests/test_trade_confirmations.py tests/test_postgres_storage.py tests/test_api.py -q`
- [ ] Run backend full tests:
  `D:\it\t-maker\backend\.venv\Scripts\python.exe -m pytest -q`
- [ ] Run backend Ruff:
  `D:\it\t-maker\backend\.venv\Scripts\python.exe -m ruff check src tests`
- [ ] Run frontend build/test commands available in `frontend/package.json`.
- [ ] Commit any final fixes.
