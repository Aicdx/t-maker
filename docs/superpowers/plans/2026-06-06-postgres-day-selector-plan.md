# PostgreSQL Day Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add PostgreSQL-backed minute-bar and T-point persistence, plus a top-of-chart date selector with previous/next trading-day navigation.

**Architecture:** Keep provider logic as the source fetcher and add a small repository/cache layer below API endpoints. API reads PostgreSQL first, fetches Tencent data on cache miss, persists bars and replay points, then returns the same chart payload shape the frontend already consumes.

**Tech Stack:** FastAPI, Pydantic, psycopg 3, PostgreSQL 16-compatible SQL, React/Vite, lightweight-charts.

---

### Task 1: PostgreSQL Schema And Repository

**Files:**
- Create: `backend/src/tmaker/storage/postgres.py`
- Create: `backend/src/tmaker/storage/__init__.py`
- Modify: `backend/src/tmaker/config.py`
- Modify: `backend/pyproject.toml`
- Test: `backend/tests/test_postgres_storage.py`

- [ ] Write repository tests for schema SQL, minute bar upsert/read, trading-day listing, and replay point upsert/read.
- [ ] Add `psycopg[binary]` dependency and `database_url` setting.
- [ ] Implement schema creation and repository methods with parameterized SQL.
- [ ] Run `pytest backend/tests/test_postgres_storage.py -q`.

### Task 2: API Day Endpoints

**Files:**
- Modify: `backend/src/tmaker/api/app.py`
- Modify: `backend/src/tmaker/strategy/replay.py`
- Test: `backend/tests/test_api.py`

- [ ] Add failing API tests for `GET /api/trading-days`, `GET /api/day`, and `POST /api/day/replay`.
- [ ] Add a date-aware provider adapter that fetches historical data and filters one trading day.
- [ ] Make `/api/day` read DB first, fetch/provider-cache on miss, and return `chart_series`, `quote`, `points`, and `provider_health`.
- [ ] Make `/api/day/replay` run strict replay for one symbol/date and persist points.
- [ ] Run `pytest backend/tests/test_api.py -q`.

### Task 3: Frontend Date Selector

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.css`
- Test: `frontend/tests/charting.test.ts` only if helper logic is extracted.

- [ ] Add API clients for trading-day list, day payload, and day replay.
- [ ] Add top-of-chart date picker with previous/next buttons.
- [ ] Load selected symbol's trading days automatically and select latest day.
- [ ] On date change, request `/api/day` and render returned chart data and stored points.
- [ ] Keep existing `复核今日` animation unchanged for today-only AI playback in this slice.
- [ ] Run `pnpm lint` and `pnpm build`.

### Task 4: Local Database Bootstrap And Verification

**Files:**
- Modify: `backend/.env`

- [ ] Create database `t_maker` on `127.0.0.1:15432` if missing.
- [ ] Add `DATABASE_URL=postgresql://postgres:191362688@127.0.0.1:15432/t_maker` to backend env.
- [ ] Start or reuse backend/frontend dev servers.
- [ ] Verify the app can select dates without pressing `五日回放`.
- [ ] Verify missing data is fetched once then served from PostgreSQL.
