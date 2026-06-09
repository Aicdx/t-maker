# Auto Monitor With Feishu And Codex Analysis Design

## Purpose

Build a backend-driven automatic watch service for the local A-share T+0 assistant. During A-share trading sessions, the backend polls watched symbols, reuses the existing rule and AI review pipeline, adds a Codex-style second analysis, and sends a Feishu notification when a meaningful buy or sell review appears.

The feature remains decision-support only. It does not connect to broker accounts, place orders, click trading software, or treat any model output as guaranteed investment advice.

## Scope

### In Scope

- Backend automatic monitoring during A-share trading time.
- Configurable polling interval.
- Reuse of existing `/api/snapshot` refresh behavior, rule engine, and structured AI review.
- A second AI analysis step that produces concise discretionary judgement for the Feishu message.
- Feishu custom robot webhook notification.
- Notification deduplication for the same symbol, timestamp, action, and model result.
- Basic monitor status APIs for start, stop, status, and Feishu test.
- Tests for signal filtering, deduplication, Feishu payload formatting, and monitor state.

### Out Of Scope

- Automatic order placement.
- Broker integration.
- Long-running operation when the backend process is not running.
- Guaranteed delivery from Feishu or public market data providers.
- Full alert routing to multiple channels such as WeChat, email, or DingTalk.
- A live Codex desktop session personally inspecting every chart window. The backend second analysis instead codifies the desired Codex-style judgement in a model call.

## Existing System Fit

The current backend already has the core signal path:

- `TencentMarketProvider` fetches realtime minute data and quotes.
- `_refresh_from_provider` refreshes candles, quotes, rule signals, and AI review.
- `evaluate_signal` emits `candidate_buy`, `candidate_sell`, `suspected`, or `hold`.
- `LlmReviewer` validates structured AI review output.
- `_upsert_signal` keeps the latest signal list stable and avoids duplicate entries by symbol and timestamp.

The automatic monitor should wrap this existing path instead of duplicating strategy logic. The monitor runner should call a small reusable service function that performs the same refresh work as `/api/snapshot`, then inspects newly reviewed signals.

## Architecture

### Monitor Runner

`tmaker.monitor.runner` owns the background loop.

Responsibilities:

- Start only when enabled by config or by `POST /api/monitor/start`.
- Sleep according to `MONITOR_INTERVAL_SECONDS`.
- Check A-share trading time before each polling cycle.
- Call the existing snapshot refresh service.
- Collect newly eligible signals.
- Pass eligible signals to second analysis and Feishu notification.
- Keep lightweight runtime state:
  - running status
  - last tick time
  - last success time
  - last error
  - last notified signal key
  - total notification count

The runner should not use global threads directly inside tests. The app factory should accept injected monitor dependencies so tests can run the tick logic synchronously.

### Monitor Policy

`tmaker.monitor.policy` decides whether a signal should notify.

Default eligibility:

- Signal kind is `candidate_buy`, `candidate_sell`, or `suspected`.
- Signal has `llm_status == "ok"`.
- Structured AI action is `buy` or `sell`.
- Structured AI confidence is at least `MONITOR_MIN_AI_CONFIDENCE`.
- Source is fresh.
- Signal key has not been notified before.

Configurable behavior:

- `MONITOR_NOTIFY_HOLD=false` by default. If enabled, AI `hold` reviews can notify only when the original rule signal was a candidate and confidence passes the threshold.
- `MONITOR_NOTIFY_SUSPECTED=true` by default, because suspected points can become useful early warnings after AI confirmation.
- `MONITOR_DEDUP_WINDOW_MINUTES` can bound in-memory dedup state to prevent unbounded growth.

Signal key:

```text
symbol | timestamp | action | llm_action | rounded_llm_confidence
```

This allows a previously pending or failed signal to notify later if it receives a real AI result.

### Codex-Style Analysis

`tmaker.llm.codex_analysis` adds a second model call after structured AI review passes monitor policy.

Input:

- Watched stock symbol and display name.
- Latest quote and signal price.
- Latest 30 one-minute candles.
- Latest 5-minute aggregate candles if available.
- Structured rule signal.
- Structured AI review result.
- Position context.
- Recent candidate signal history.
- Provider health and market context when available.

Output schema:

```json
{
  "judgement": "buy | sell | wait | avoid",
  "summary": "one concise paragraph",
  "key_levels": ["support/resistance/invalid level"],
  "next_steps": ["what to watch next"],
  "invalidates": ["conditions that invalidate this point"],
  "risk_notes": ["risk notes"]
}
```

Style requirements:

- Use practical intraday language.
- Explicitly state whether this is confirmation, observation, or a wait condition.
- Avoid deterministic profit promises.
- Mention invalidation conditions and waiting conditions.
- Keep the output short enough for a Feishu message.

If the second analysis fails, the Feishu notification should still be sent with the structured AI review and a short note: `Codex 二次分析暂不可用`.

### Feishu Notifier

`tmaker.notify.feishu` sends a Feishu custom robot message.

Configuration:

- `FEISHU_WEBHOOK_URL`
- Optional `FEISHU_WEBHOOK_SECRET` if signing is needed later.
- `FEISHU_TIMEOUT_SECONDS`

Message format:

```text
【T Maker 盯盘复核】中际旭创 300308

信号：低吸候选
时间：10:23
价格：123.45
规则置信度：72%
工程 AI：低吸，68%

工程 AI 结论：
...

Codex 二次判断：
...

关键价位：
- ...

等待确认：
- ...

失效条件：
- ...

风险：
- ...

提醒：仅供盘中辅助判断，不自动下单。
```

The notifier should use plain text first because it is stable and easy to test. Rich Feishu card messages can be added later without changing monitor policy.

### API Surface

Add monitor endpoints under FastAPI:

- `GET /api/monitor/status`
  - Returns running status, enabled config, last tick, last success, last error, notification count, last notified signal.
- `POST /api/monitor/start`
  - Starts the background monitor loop if not already running.
- `POST /api/monitor/stop`
  - Stops the background monitor loop.
- `POST /api/monitor/test-feishu`
  - Sends a test message to the configured Feishu webhook.

The existing `/api/snapshot` endpoint remains available and should not depend on monitor being enabled.

## Data Flow

1. Backend starts.
2. If `MONITOR_AUTO_START=true`, FastAPI startup starts the monitor runner.
3. On each trading-time tick, monitor calls the existing refresh service.
4. Refresh service updates candles, quotes, rule signals, and structured AI reviews.
5. Monitor policy filters new reviewed signals.
6. For each eligible signal, Codex-style analysis runs.
7. Feishu notifier sends a composed message.
8. Dedup state records the signal key only after notification succeeds.
9. Monitor state updates last success or last error.

## Error Handling

- Market data failure:
  - Store the error in monitor status.
  - Do not send buy/sell notifications from stale data.
- Structured AI review failure:
  - Do not send the normal trade alert.
  - Keep existing frontend behavior showing `llm_status=failed`.
- Codex-style analysis failure:
  - Send Feishu notification with structured AI result and a fallback note.
- Feishu failure:
  - Keep the signal unmarked in notification dedup so a later tick can retry.
  - Store the latest Feishu error in monitor status.
- Repeated failures:
  - Do not crash the backend.
  - Continue polling on the next interval.

## Configuration

Add settings with conservative defaults:

```env
MONITOR_AUTO_START=false
MONITOR_INTERVAL_SECONDS=30
MONITOR_MIN_AI_CONFIDENCE=0.60
MONITOR_NOTIFY_HOLD=false
MONITOR_NOTIFY_SUSPECTED=true
MONITOR_DEDUP_WINDOW_MINUTES=240
CODEX_ANALYSIS_ENABLED=true
FEISHU_WEBHOOK_URL=
FEISHU_TIMEOUT_SECONDS=8
```

`MONITOR_AUTO_START` defaults to false so local development and tests do not unexpectedly call public market data, model APIs, or Feishu. The user can enable it in `.env` once the webhook and model settings are ready.

## Testing

Backend unit tests:

- Monitor policy accepts AI-confirmed buy/sell signals above threshold.
- Monitor policy rejects hold by default.
- Monitor policy rejects stale source signals.
- Dedup prevents repeated notifications for the same reviewed signal.
- Feishu notifier formats expected text fields.
- Feishu notifier handles HTTP errors.
- Codex-style analysis parser accepts valid schema and returns fallback on failure.
- Monitor tick sends one notification for one eligible signal.
- Monitor tick does not mark dedup when Feishu fails.

API tests:

- `GET /api/monitor/status` returns monitor state.
- `POST /api/monitor/start` starts idempotently.
- `POST /api/monitor/stop` stops idempotently.
- `POST /api/monitor/test-feishu` reports missing webhook as a clear configuration error.

Manual verification:

- Configure `.env` with OpenAI-compatible model and Feishu webhook.
- Start backend.
- Call test Feishu endpoint.
- Enable monitor and verify no notifications occur outside trading time.
- During trading time, verify a qualifying signal sends one Feishu message with both engineering AI review and Codex-style analysis.

## Acceptance Criteria

- Backend can run automatic monitoring without the browser page being open.
- Monitoring uses existing signal and AI review behavior instead of a separate strategy implementation.
- Feishu notification includes structured engineering AI review and Codex-style second judgement.
- Duplicate notifications are suppressed.
- Notifications are not sent for stale data.
- Feishu failures do not crash the backend.
- Monitor can be started, stopped, and inspected through API endpoints.
- No code path places orders or interacts with broker software.
