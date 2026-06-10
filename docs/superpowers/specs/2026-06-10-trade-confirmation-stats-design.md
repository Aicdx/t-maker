# Trade Confirmation Stats Design

## Purpose

Let the user manually confirm an AI buy or sell point from the dashboard and persist that confirmation in PostgreSQL. The app then calculates a same-day T+0 spread summary assuming each confirmation represents 100 shares.

The feature is a journal and statistics aid only. It does not place orders, connect to a broker, or infer that an AI point was actually executed unless the user clicks the confirmation button.

## Scope

### In Scope

- Persist manual "already bought low" and "already sold high" confirmations to PostgreSQL.
- Record the selected AI point that the user is confirming.
- Treat each confirmation as 100 shares.
- Calculate paired spread PnL per symbol:
  - low buy then high sell: `(sell_price - buy_price) * 100`
  - high sell then low buy: `(sell_price - buy_price) * 100`
- Default the statistics page/API to today's trading date.
- Support a future/history date parameter: `?date=YYYY-MM-DD`.
- Show summary metrics, paired trades, and unpaired confirmations in the frontend.
- Allow deletion of a mistaken confirmation.
- Keep all behavior manual. No automatic order or broker integration.

### Out Of Scope

- Brokerage order placement or account synchronization.
- Fees, stamp duty, slippage, or exact exchange settlement modeling.
- Multi-lot inventory accounting beyond simple chronological pairing.
- Cross-day pairing. Each stats request pairs only records for the selected date.
- Multi-user permissions. This remains a local single-user app.

## Existing System Fit

The backend already uses `PostgresRepository` with `SCHEMA_SQL`, model conversion helpers, and app-factory dependency injection for tests. The new confirmation table should live beside `t_signal_points` and use the same repository pattern.

The frontend already has a right-side decision panel with:

- `ReplayPointCard` for the selected AI point.
- static buttons labeled "已低吸" and "已高抛".
- existing replay/monitor point shapes that contain symbol, timestamp, action, price, reason, rule IDs, and LLM fields.

The feature should wire those existing buttons to a backend API instead of introducing a separate manual entry form.

## Data Model

### Table: `t_trade_confirmations`

Columns:

- `id UUID PRIMARY KEY`
- `symbol TEXT NOT NULL`
- `trade_date DATE NOT NULL`
- `signal_timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL`
- `signal_action TEXT NOT NULL`
- `confirm_action TEXT NOT NULL`
- `price NUMERIC NOT NULL`
- `quantity INTEGER NOT NULL DEFAULT 100`
- `source TEXT NOT NULL`
- `reason TEXT NOT NULL`
- `llm_confidence NUMERIC`
- `created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()`

Indexes:

- `idx_t_trade_confirmations_date_symbol` on `(trade_date, symbol, signal_timestamp, created_at)`

Constraints:

- `confirm_action IN ('buy', 'sell')`
- `signal_action IN ('buy', 'sell', 'hold')`
- `price >= 0`
- `quantity > 0`

The API generates `id` server-side. The frontend does not need to choose IDs.

## API

### `POST /api/trade-confirmations`

Request:

```json
{
  "symbol": "300308",
  "signal_timestamp": "2026-06-10T10:24:00",
  "signal_action": "buy",
  "confirm_action": "buy",
  "price": 123.45,
  "quantity": 100,
  "source": "monitor",
  "reason": "AI低吸点位",
  "llm_confidence": 0.72
}
```

Behavior:

- Validate actions and price.
- Default `quantity` to `100`.
- Derive `trade_date` from `signal_timestamp`.
- Persist the record.
- Return the saved confirmation.

### `GET /api/trade-confirmations/stats`

Query:

- `date` optional, default today in server local date.

Behavior:

- Load confirmations for the requested date.
- Group by `symbol`.
- Sort by `signal_timestamp`, then `created_at`.
- Pair each buy with the next opposite sell for the same symbol.
- Pair each sell with the next opposite buy for the same symbol.
- Calculate `spread = sell_price - buy_price`.
- Calculate `pnl = spread * quantity`; current scope keeps `quantity` fixed at 100 for every record.
- Return summary totals, paired trade rows, and unpaired confirmation rows.

Response shape:

```json
{
  "date": "2026-06-10",
  "quantity_per_trade": 100,
  "summary": {
    "record_count": 4,
    "paired_count": 2,
    "unpaired_count": 0,
    "total_pnl": 156.0
  },
  "pairs": [
    {
      "symbol": "300308",
      "buy_id": "...",
      "sell_id": "...",
      "buy_price": 123.45,
      "sell_price": 125.01,
      "quantity": 100,
      "spread": 1.56,
      "pnl": 156.0,
      "opened_at": "2026-06-10T10:24:00",
      "closed_at": "2026-06-10T13:12:00"
    }
  ],
  "unpaired": []
}
```

### `DELETE /api/trade-confirmations/{id}`

Behavior:

- Delete one confirmation by ID.
- Return `{"status": "ok"}`.
- If the ID is absent, return 404.

## Pairing Rules

Pairing is intentionally simple and auditable:

1. Process records per symbol.
2. Sort by signal time, then creation time.
3. Keep one FIFO pending buy queue and one FIFO pending sell queue.
4. When a buy arrives:
   - If a pending sell exists, pair with the oldest pending sell.
   - Otherwise queue the buy.
5. When a sell arrives:
   - If a pending buy exists, pair with the oldest pending buy.
   - Otherwise queue the sell.
6. PnL always uses `sell_price - buy_price`, so sell-then-buy can produce a positive number when the later buy is lower.

This makes the stats explainable when the user reviews the raw confirmation list.

## Frontend Design

The decision panel keeps the existing workflow:

- Select an AI point from the chart/replay strip.
- Click "已低吸" or "已高抛".
- The button posts the selected point to the backend.
- On success, the stats section refreshes.

Button states:

- Disabled when no AI point is selected.
- Disabled while saving.
- Show inline error if the save fails.
- Allow direction mismatch, but label it in the raw record as manual confirmation. This supports real discretionary decisions.

Stats section:

- Add a compact "做T统计" section below the decision panel or as an adjacent dashboard band.
- Default date is today.
- Show metrics:
  - total spread PnL
  - paired count
  - unpaired count
  - record count
- Show recent pair rows with buy/sell prices, spread, and PnL.
- Show unpaired rows so the user knows which confirmations still need the opposite side.
- Provide a delete button per raw/unpaired row and pair member where feasible.

## Error Handling

- API returns 422 for invalid price, action, timestamp, or quantity.
- API returns 404 when deleting a missing confirmation.
- API returns 503 only if database access fails before a controlled result can be produced.
- Frontend keeps the selected AI point after a failed save and shows the error inline.
- Frontend refetches stats after successful save or delete.

## Testing

Backend:

- Schema includes `t_trade_confirmations`.
- Repository can save, list by date, and delete confirmations.
- Stats pairing covers buy-then-sell, sell-then-buy, per-symbol isolation, and unpaired records.
- API defaults stats to today when `date` is omitted.
- API honors `?date=YYYY-MM-DD`.

Frontend:

- A pure stats helper formats summary and PnL values.
- Clicking "已低吸" posts the selected AI point with `confirm_action: "buy"`.
- Clicking "已高抛" posts with `confirm_action: "sell"`.
- The stats panel renders paired and unpaired rows.
- Save errors display inline without clearing the selected point.
