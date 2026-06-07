# A-Share T+0 Assistant Design

## Purpose

Build a local A-share intraday T+0 assistant for 1-5 watched stocks. The assistant monitors 1-minute quotes, derives 5-minute candles, detects candidate buy/sell points, and calls an OpenAI-compatible model only when a rule-based candidate or suspicious point appears.

The first version is a decision-support tool only. It does not place orders, connect to broker accounts, or promise investment outcomes. Every signal must remain an informational prompt that requires human confirmation.

## Scope

### In Scope

- A-share watchlist with 1-5 manually selected stocks.
- Free data source first, using AKShare wrappers around public A-share quote endpoints such as Eastmoney.
- Local Python FastAPI backend.
- React web dashboard.
- 1-minute candle ingestion and cache.
- Derived 5-minute candles from the 1-minute stream.
- Manual position state:
  - base position quantity
  - cost price
  - available cash
  - planned T trade quantity
- Rule-based signal engine.
- OpenAI-compatible model review for rule candidates.
- Signal log and manual operation records.
- Historical 1-minute replay for strategy verification.

### Out Of Scope For V1

- Automatic order placement.
- Broker integration.
- Full-market scanning.
- Complex tax, fee, and realized PnL accounting.
- Guaranteed real-time reliability from free quote sources.
- Investment advice or profitability claims.

## Architecture

### Components

- `market-data`
  - Pulls A-share minute data through AKShare.
  - Normalizes symbols, timestamps, OHLCV fields, and trading-session boundaries.
  - Tracks source health, latency, duplicate candles, and missing candles.

- `bar-builder`
  - Maintains an in-memory rolling 1-minute candle buffer per symbol.
  - Builds 5-minute candles from complete 1-minute groups.
  - Persists enough recent candles for restart recovery and replay.

- `indicator-engine`
  - Computes VWAP/intraday average price.
  - Computes price/VWAP deviation, volume ratios, RSI/KDJ, MACD momentum, Bollinger/ATR deviation, OBV, and relative strength vs market index.
  - Produces a compact indicator snapshot for rules and model review.

- `signal-engine`
  - Converts strategy rules into deterministic candidate signals.
  - Scores each candidate by rule alignment, trend confirmation, volume behavior, and risk filters.
  - Emits `candidate_buy`, `candidate_sell`, `suspected`, or `hold`.

- `llm-review`
  - Calls an OpenAI-compatible API only for candidate or suspected signals.
  - Requires structured JSON output.
  - Does not run every minute for every stock.

- `portfolio-state`
  - Stores manual position, cost, available cash, and planned T quantity.
  - Adds position context to every signal.

- `api-server`
  - FastAPI REST endpoints for configuration and historical state.
  - WebSocket or Server-Sent Events for live candles, indicators, and signals.

- `web-dashboard`
  - React application for watchlist, charting, position inputs, signals, model review, and manual operation logs.

## Data Source Strategy

V1 uses AKShare as the first integration layer because it gives Python-friendly access to public A-share quote endpoints. Public/free endpoints can change, throttle, or lag, so the market data layer must hide provider details behind an adapter interface.

Provider health must be visible in the UI:

- last successful update time
- quote latency
- stale-data status
- missing candle count
- model-review failure state

If data for a symbol is stale beyond the configured threshold, the system must stop creating new buy/sell signals for that symbol and show `data_delayed`.

## Signal Design

Each signal must include:

- symbol
- timestamp
- signal side: `buy`, `sell`, or `hold`
- confidence score
- matched rule ids
- human-readable reason
- risk notes
- source data freshness
- whether LLM review was requested
- LLM review result, if available

### User-Provided Rules

#### Low-Level Rising Bottoms

Detect open-session low-level consolidation where multiple pullbacks do not break the previous low and local lows rise gradually.

Candidate buy trigger:

- at least three local lows after market open
- each low is not lower than the previous low by more than a tolerance
- the third low forms near VWAP support or a short-term support band
- price begins to turn upward from the third low
- 5-minute trend is not strongly bearish

#### Sharp Drop With Shrinking Volume

Detect intraday sharp drops where price falls quickly but sell volume shrinks.

Candidate buy trigger:

- short-window drop exceeds a configured threshold
- recent volume contracts during the falling segment
- price is far below VWAP or intraday average price
- the latest candle shows stabilization or rebound

Candidate sell trigger after rebound:

- price rebounds toward VWAP or a short-term resistance band
- rebound momentum weakens
- T position is available to sell

#### Sharp Pull-Up Without Limit-Up

Detect early-session fast pull-ups that fail to seal limit-up or fail to hold the high area.

Candidate sell trigger:

- strong pull-up within the first half hour
- price approaches a limit-up or local high area
- high-volume rejection or obvious pullback appears
- market/index confirmation is weak

Candidate buy-back trigger:

- later fast drop stabilizes
- price returns to a support zone
- volume pressure weakens

#### VWAP High-Sell Low-Buy

Treat VWAP or intraday average price as the mean-reversion anchor.

Candidate buy trigger:

- price falls while VWAP remains flat or rising
- price deviation below VWAP is large relative to ATR/intraday volatility
- selling pressure weakens or rebound confirmation appears

Candidate sell trigger:

- price rises while VWAP fails to rise meaningfully
- price deviation above VWAP is large
- momentum weakens near prior high or resistance

#### Stock vs Market Relative Strength

Use market/index behavior as a filter.

Candidate sell bias:

- market rises but watched stock fails to rise
- watched stock underperforms its benchmark by a configured threshold
- position has sellable T quantity or base position risk should be reduced manually

Candidate buy bias:

- market pulls back but watched stock does not fall, or rises against the market
- watched stock stabilizes earlier than benchmark
- other buy rules also align

### Additional Filters

The following indicators are used as filters and scoring inputs, not isolated trade commands:

- RSI/KDJ for short-term overbought or oversold conditions.
- MACD histogram and slope for momentum change.
- Bollinger Band and ATR deviation for excessive price distance.
- Volume ratio and OBV for accumulation/distribution pressure.
- 5-minute candle direction for confirmation.
- Previous intraday high/low and support/resistance levels.

## LLM Review

### Trigger

Call the model only when the rule engine emits:

- `candidate_buy`
- `candidate_sell`
- `suspected`

Do not call the model for ordinary `hold` states.

### Input Context

The review payload should include:

- stock symbol and display name
- latest price and timestamp
- latest 30-60 1-minute candles
- latest 12-24 5-minute candles
- VWAP and price deviation
- volume trend and volume ratio
- relative strength vs index
- matched rule ids and rule explanations
- manual position state
- current risk filters
- recent signal history for the same symbol

### Output Schema

The model must return structured JSON:

```json
{
  "action": "buy | sell | hold",
  "confidence": 0.0,
  "summary": "short decision summary",
  "reasons": ["reason 1", "reason 2"],
  "risks": ["risk 1", "risk 2"],
  "wait_for": ["condition 1", "condition 2"]
}
```

If the model call fails, times out, or returns invalid JSON, the signal remains a rule-only candidate with `llm_status = failed`.

## Dashboard Design

### Main Layout

- Left panel:
  - watched stock list
  - current state per symbol
  - data freshness badge
  - latest signal badge

- Center panel:
  - 1-minute candlestick chart
  - derived 5-minute confirmation overlay or secondary chart
  - VWAP/intraday average price
  - buy/sell markers
  - indicator cards
  - signal timeline

- Right panel:
  - manual position form
  - current rule signal
  - LLM review card
  - action record buttons: bought, sold, ignored
  - latest risk warnings

### Manual Records

Manual records are not broker executions. They are local annotations used for later review:

- timestamp
- symbol
- action: `bought`, `sold`, `ignored`
- quantity
- price
- note
- related signal id

## Risk Controls

- Never place orders automatically.
- Mark all prompts as decision-support information.
- Pause new signals if data is stale.
- Reduce or suppress new entry signals near lunch break and market close.
- Suppress chase signals near limit-up or limit-down states.
- Pause a symbol after configured consecutive wrong/ignored signals or daily loss annotations.
- Require every buy/sell prompt to include reasons and risks.
- Show whether the model reviewed the candidate or not.

## Testing And Verification

### Historical Replay

Replay historical 1-minute data through the same ingestion, indicator, rule, and model-review interfaces. Verify that known patterns trigger expected candidate signals.

### Synthetic Scenarios

Create deterministic synthetic candle streams for:

- rising-bottom low-level consolidation
- sharp drop with shrinking volume
- sharp pull-up and failed high hold
- VWAP high deviation
- VWAP low deviation
- stock underperforming a rising index
- stock outperforming a falling index
- stale data and missing candle cases

### API Tests

Verify:

- watchlist CRUD
- manual position updates
- candle normalization
- 5-minute aggregation
- rule engine outputs
- LLM JSON validation and failure fallback
- WebSocket/SSE event format

### Frontend Checks

Verify:

- chart renders for selected symbol
- signal badges update in real time
- LLM review card handles success/failure
- stale-data state is visible
- manual operation records appear in timeline

## Configuration

Expected local configuration:

```env
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=
OPENAI_MODEL=
MARKET_DATA_PROVIDER=akshare
POLL_INTERVAL_SECONDS=15
STALE_DATA_SECONDS=90
WATCHLIST_MAX_SIZE=5
```

The implementation should support `.env` for local development and avoid committing secrets.

## Acceptance Criteria

- User can configure 1-5 A-share symbols.
- User can enter position, cost, cash, and planned T quantity.
- The backend pulls or replays 1-minute data and builds 5-minute candles.
- The dashboard displays 1-minute/5-minute context and VWAP.
- The rule engine emits explainable buy/sell/hold signals.
- The model is called only for candidate or suspected signals.
- Model output is parsed as structured JSON and displayed.
- Data delays suppress new signals and are visible in the UI.
- Manual operation records are saved and shown in the signal timeline.
- The system runs locally and does not place trades.

