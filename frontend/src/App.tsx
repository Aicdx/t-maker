import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ChartLineUp,
  ClockCounterClockwise,
  Database,
  CalendarBlank,
  CaretLeft,
  CaretRight,
  Pulse,
  WarningCircle,
} from '@phosphor-icons/react'
import { DayPicker } from 'react-day-picker'
import { zhCN } from 'react-day-picker/locale'
import 'react-day-picker/style.css'
import './App.css'
import { apiErrorMessage } from './apiErrors'
import { quoteForVisibleContext } from './quoteState'
import {
  buildChartPoints,
  buildQuoteSummary,
  buildReplayMarkers,
  markerColor,
  type ChartMarker,
  type QuoteSummary,
} from './charting'
import { FinancialChart } from './FinancialChart'
import { isAshareTradingTime, monitorStatus, type MonitorStatus } from './marketHours'
import {
  chartTradeDateLabel,
  dayMarketPayloadToReplay,
  replayPointReviewLabel,
  replaySourceLabel,
  replaySummaryFromPoints,
  shiftCalendarDate,
  type ReplayPoint,
  type ReplayResult,
  type ReplaySummary,
} from './replayState'
import {
  buildTradeConfirmationRequest,
  formatTradeMoney,
  tradeConfirmationActionLabel,
  type TradeConfirmationAction,
  type TradeConfirmationStats,
} from './tradeStats'

type WatchSymbol = {
  symbol: string
  name: string
  status: string
}

type Position = {
  symbol: string
  base_quantity: number
  cost_price: number
  available_cash: number
  t_quantity: number
}

type Candle = {
  symbol: string
  timestamp: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

type Signal = {
  symbol: string
  timestamp: string
  kind: 'candidate_buy' | 'candidate_sell' | 'suspected' | 'hold'
  action: 'buy' | 'sell' | 'hold'
  confidence: number
  rule_ids: string[]
  reason: string
  risks: string[]
  source_fresh: boolean
  llm_status: string
  llm_review?: LlmReview | null
}

type SymbolReplayResult = {
  symbol: string
  date: string
  mode: 'strict' | 'optimized'
  strict: boolean
  chart_series: Snapshot['chart_series']
  points: ReplayPoint[]
  summary: AppReplaySummary
}

type RecentReplayDay = {
  date: string
  mode: 'strict' | 'optimized'
  strict: boolean
  chart_series: Snapshot['chart_series']
  points: ReplayPoint[]
  summary: AppReplaySummary
}

type AppReplaySummary = ReplaySummary & {
  ai_buy_count?: number
  ai_sell_count?: number
  ai_hold_count?: number
  accuracy_checked_count?: number
  accuracy_hit_count?: number
  accuracy_rate_pct?: number | null
  trading_day_count?: number
}

type AppReplayResult = ReplayResult & {
  summary: AppReplaySummary
  artifact_path?: string
}

type RecentReplayResult = {
  days_requested: number
  mode: 'strict' | 'optimized'
  strict: boolean
  review_enabled: boolean
  symbols: string[]
  days: RecentReplayDay[]
  summary: AppReplaySummary
  artifact_path?: string
}

type TradingDaysResult = {
  symbol: string
  days: string[]
}

type TradingDayPayload = {
  symbol: string
  date: string
  chart_series: Snapshot['chart_series']
  points: ReplayPoint[]
  quote?: MarketQuote | null
  provider_health: ProviderHealth
  mode?: 'strict' | 'optimized'
  strict?: boolean
  summary?: AppReplaySummary
}

type LlmReview = {
  action: 'buy' | 'sell' | 'hold'
  confidence: number
  summary: string
  reasons: string[]
  risks: string[]
  wait_for: string[]
  execution_allowed?: boolean
  execution_blockers?: string[]
}

type ProviderHealth = {
  provider: string
  symbol: string
  last_success_at: string | null
  latency_ms: number | null
  stale_after_seconds: number
  missing_candle_count: number
  last_error: string | null
}

type MarketQuote = {
  symbol: string
  name: string
  latest: number
  previous_close: number
  open: number
  high: number
  low: number
  change: number
  change_percent: number
}

type Snapshot = {
  watchlist: WatchSymbol[]
  positions: Position[]
  candles: Candle[]
  quotes?: Record<string, MarketQuote>
  chart_series: {
    realtime: Candle[]
    one_minute: Candle[]
    five_minute: Candle[]
  }
  signals: Signal[]
  provider_health: ProviderHealth
}

type ChartMode = 'realtime' | 'one_minute' | 'five_minute'

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000'

async function fetchSnapshot(signal?: AbortSignal) {
  const response = await fetch(`${API_BASE}/api/snapshot`, { signal })
  if (!response.ok) throw new Error(await apiErrorMessage(response))
  return (await response.json()) as Snapshot
}

async function fetchDaySymbolReplay(symbol: string, date: string) {
  const response = await fetch(
    `${API_BASE}/api/day/replay?symbol=${encodeURIComponent(symbol)}&date=${encodeURIComponent(
      date,
    )}&strict=true&review=false`,
    { method: 'POST' },
  )
  if (!response.ok) throw new Error(await apiErrorMessage(response))
  return (await response.json()) as SymbolReplayResult
}

async function reviewDayReplayPoint(symbol: string, date: string, timestamp: string) {
  const response = await fetch(
    `${API_BASE}/api/day/replay/review?symbol=${encodeURIComponent(symbol)}&date=${encodeURIComponent(
      date,
    )}&timestamp=${encodeURIComponent(timestamp)}&strict=true`,
    { method: 'POST' },
  )
  if (!response.ok) throw new Error(await apiErrorMessage(response))
  return (await response.json()) as ReplayPoint
}

async function fetchTradingDays(symbol: string) {
  const response = await fetch(`${API_BASE}/api/trading-days?symbol=${encodeURIComponent(symbol)}`)
  if (!response.ok) throw new Error(await apiErrorMessage(response))
  return (await response.json()) as TradingDaysResult
}

async function fetchTradingDay(symbol: string, date: string) {
  const response = await fetch(
    `${API_BASE}/api/day?symbol=${encodeURIComponent(symbol)}&date=${encodeURIComponent(date)}`,
  )
  if (!response.ok) throw new Error(await apiErrorMessage(response))
  return (await response.json()) as TradingDayPayload
}

async function fetchTradeConfirmationStats(date?: string, signal?: AbortSignal) {
  const query = date ? `?date=${encodeURIComponent(date)}` : ''
  const response = await fetch(`${API_BASE}/api/trade-confirmations/stats${query}`, { signal })
  if (!response.ok) throw new Error(await apiErrorMessage(response))
  return (await response.json()) as TradeConfirmationStats
}

async function createTradeConfirmation(point: ReplayPoint, action: TradeConfirmationAction, source: string) {
  const response = await fetch(`${API_BASE}/api/trade-confirmations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(buildTradeConfirmationRequest(point, action, source)),
  })
  if (!response.ok) throw new Error(await apiErrorMessage(response))
}

async function deleteTradeConfirmation(id: string) {
  const response = await fetch(`${API_BASE}/api/trade-confirmations/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })
  if (!response.ok) throw new Error(await apiErrorMessage(response))
}

function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedSymbol, setSelectedSymbol] = useState('300308')
  const [chartMode, setChartMode] = useState<ChartMode>('realtime')
  const [hoveredPoint, setHoveredPoint] = useState<ReturnType<typeof buildChartPoints>[number] | null>(null)
  const [replay, setReplay] = useState<AppReplayResult | null>(null)
  const [recentReplay, setRecentReplay] = useState<RecentReplayResult | null>(null)
  const [selectedReplayDate, setSelectedReplayDate] = useState<string | null>(null)
  const [tradingDays, setTradingDays] = useState<string[]>([])
  const [selectedTradeDate, setSelectedTradeDate] = useState<string>('')
  const [selectedDayPayload, setSelectedDayPayload] = useState<TradingDayPayload | null>(null)
  const [dayLoading, setDayLoading] = useState(false)
  const [replayReviewLoading, setReplayReviewLoading] = useState(false)
  const [selectedReplayKey, setSelectedReplayKey] = useState<string | null>(null)
  const [monitorEnabled, setMonitorEnabled] = useState(false)
  const [monitorNow, setMonitorNow] = useState(() => new Date())
  const [monitorLastPulledAt, setMonitorLastPulledAt] = useState<string | null>(null)
  const [playbackCandles, setPlaybackCandles] = useState<Candle[] | null>(null)
  const [playbackPoints, setPlaybackPoints] = useState<ReplayPoint[]>([])
  const [playbackQueue, setPlaybackQueue] = useState<ReplayPoint[]>([])
  const [playbackIndex, setPlaybackIndex] = useState(0)
  const [playbackStatus, setPlaybackStatus] = useState<'idle' | 'playing' | 'reviewing' | 'done'>('idle')
  const [playbackSymbol, setPlaybackSymbol] = useState<string | null>(null)
  const [playbackDate, setPlaybackDate] = useState<string | null>(null)
  const [tradeStats, setTradeStats] = useState<TradeConfirmationStats | null>(null)
  const [tradeStatsLoading, setTradeStatsLoading] = useState(false)
  const [tradeStatsError, setTradeStatsError] = useState('')
  const [tradeSavingAction, setTradeSavingAction] = useState<TradeConfirmationAction | null>(null)
  const [deletingTradeId, setDeletingTradeId] = useState<string | null>(null)
  const lastLoadedDaysSymbolRef = useRef<string | null>(null)
  const isReplaying = replayReviewLoading || dayLoading
  const currentMonitorStatus = monitorStatus(monitorEnabled, monitorNow)

  const applySnapshotPayload = useCallback((payload: Snapshot) => {
    setSnapshot(payload)
    setSelectedSymbol((current) =>
      payload.watchlist.some((item) => item.symbol === current)
        ? current
        : (payload.watchlist[0]?.symbol ?? '300308'),
    )
  }, [])

  const loadTradeStats = useCallback(async (date?: string, signal?: AbortSignal) => {
    try {
      setTradeStatsLoading(true)
      setTradeStatsError('')
      const stats = await fetchTradeConfirmationStats(date, signal)
      if (signal?.aborted) return
      setTradeStats(stats)
    } catch (err) {
      if (signal?.aborted) return
      setTradeStatsError(err instanceof Error ? err.message : '做T统计加载失败')
    } finally {
      if (!signal?.aborted) setTradeStatsLoading(false)
    }
  }, [])

  async function confirmSelectedTrade(action: TradeConfirmationAction) {
    if (!selectedReplayPoint) {
      setTradeStatsError('请先选择一个 AI 低吸或高抛点位')
      return
    }
    try {
      setTradeSavingAction(action)
      setTradeStatsError('')
      await createTradeConfirmation(selectedReplayPoint, action, monitorEnabled ? 'monitor' : 'replay')
      await loadTradeStats(selectedReplayPoint.timestamp.slice(0, 10))
    } catch (err) {
      setTradeStatsError(err instanceof Error ? err.message : '确认点位保存失败')
    } finally {
      setTradeSavingAction(null)
    }
  }

  async function removeTradeConfirmation(id: string) {
    try {
      setDeletingTradeId(id)
      setTradeStatsError('')
      await deleteTradeConfirmation(id)
      await loadTradeStats(tradeStats?.date)
    } catch (err) {
      setTradeStatsError(err instanceof Error ? err.message : '确认记录删除失败')
    } finally {
      setDeletingTradeId(null)
    }
  }

  async function runSelectedDaySymbolReview() {
    if (!selectedTradeDate) {
      setError('请先选择要复核的交易日期')
      return
    }
    const reviewSymbol = selectedSymbol
    const reviewDate = selectedTradeDate
    try {
      setReplayReviewLoading(true)
      setError('')
      setRecentReplay(null)
      setSelectedReplayDate(reviewDate)
      setHoveredPoint(null)
      setChartMode('realtime')
      const result = await fetchDaySymbolReplay(reviewSymbol, reviewDate)
      const orderedCandles = result.chart_series.realtime
        .filter((candle) => candle.symbol === reviewSymbol)
        .sort((left, right) => left.timestamp.localeCompare(right.timestamp))
      setPlaybackSymbol(reviewSymbol)
      setPlaybackDate(result.date)
      setPlaybackCandles(orderedCandles)
      setPlaybackQueue([...result.points].sort((left, right) => left.timestamp.localeCompare(right.timestamp)))
      setPlaybackPoints([])
      setPlaybackIndex(orderedCandles.length ? 1 : 0)
      setPlaybackStatus(orderedCandles.length ? 'playing' : 'done')
      setReplay({
        date: result.date,
        mode: result.mode,
        strict: result.strict,
        points: [],
        summary: result.summary,
      })
      setSelectedReplayKey(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '复核所选日期失败')
      setPlaybackStatus('idle')
    } finally {
      setReplayReviewLoading(false)
    }
  }

  async function loadTradingDays(symbol: string, preferredDate?: string) {
    try {
      setDayLoading(true)
      setError('')
      lastLoadedDaysSymbolRef.current = symbol
      const daysPayload = await fetchTradingDays(symbol)
      const days = daysPayload.days
      setTradingDays(days)
      const nextDate = preferredDate && days.includes(preferredDate) ? preferredDate : (days.at(-1) ?? '')
      setSelectedTradeDate(nextDate)
      if (nextDate) {
        await loadTradingDay(symbol, nextDate)
      } else {
        setSelectedDayPayload(null)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '交易日加载失败')
      setTradingDays([])
      setSelectedTradeDate('')
      setSelectedDayPayload(null)
    } finally {
      setDayLoading(false)
    }
  }

  async function loadTradingDay(symbol: string, date: string) {
    const payload = await fetchTradingDay(symbol, date)
    setSelectedDayPayload(payload)
    setReplay(dayMarketPayloadToReplay(payload))
    setSelectedReplayDate(payload.date)
    setRecentReplay(null)
    setSelectedReplayKey(null)
    setPlaybackStatus('idle')
    setPlaybackCandles(null)
    setPlaybackDate(null)
    setPlaybackPoints([])
    setPlaybackQueue([])
    setHoveredPoint(null)
  }

  async function changeTradeDate(date: string) {
    if (!date) return
    try {
      setDayLoading(true)
      setError('')
      setSelectedTradeDate(date)
      await loadTradingDay(selectedSymbol, date)
    } catch (err) {
      setError(err instanceof Error ? err.message : '交易日行情加载失败')
    } finally {
      setDayLoading(false)
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    fetchSnapshot(controller.signal)
      .then((payload) => {
        setHoveredPoint(null)
        applySnapshotPayload(payload)
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return
        setError(err instanceof Error ? err.message : '无法连接后端')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })

    return () => controller.abort()
  }, [applySnapshotPayload])

  useEffect(() => {
    const controller = new AbortController()
    void loadTradeStats(undefined, controller.signal)
    return () => controller.abort()
  }, [loadTradeStats])

  useEffect(() => {
    if (!monitorEnabled) return
    let inFlight = false
    let cancelled = false

    const tick = async () => {
      const now = new Date()
      setMonitorNow(now)
      if (!isAshareTradingTime(now) || inFlight) return
      inFlight = true
      try {
        const payload = await fetchSnapshot()
        if (cancelled) return
        applySnapshotPayload(payload)
        setMonitorLastPulledAt(new Date().toISOString())
        setError('')
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : '盯盘刷新失败')
      } finally {
        inFlight = false
      }
    }

    void tick()
    const timer = window.setInterval(() => void tick(), 2000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [applySnapshotPayload, monitorEnabled])

  useEffect(() => {
    if (!snapshot?.watchlist.some((item) => item.symbol === selectedSymbol)) return
    if (lastLoadedDaysSymbolRef.current === selectedSymbol) return
    lastLoadedDaysSymbolRef.current = selectedSymbol
    queueMicrotask(() => {
      void loadTradingDays(selectedSymbol, selectedTradeDate || undefined)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSymbol, snapshot?.watchlist])

  const selectedPosition = snapshot?.positions.find((item) => item.symbol === selectedSymbol)
  const selectedSignal = snapshot?.signals
    .filter((signal) => signal.symbol === selectedSymbol)
    .at(-1)
  const selectedRecentReplayDay = useMemo(
    () => recentReplay?.days.find((day) => day.date === selectedReplayDate) ?? null,
    [recentReplay?.days, selectedReplayDate],
  )
  const selectedDayReplay = useMemo(
    () =>
      selectedDayPayload
        ? ({
            date: selectedDayPayload.date,
            mode: selectedDayPayload.mode ?? 'strict',
            strict: selectedDayPayload.strict ?? true,
            chart_series: selectedDayPayload.chart_series,
            points: selectedDayPayload.points,
            summary: selectedDayPayload.summary ?? replaySummaryFromPoints(selectedDayPayload.points),
          } satisfies RecentReplayDay)
        : null,
    [selectedDayPayload],
  )
  const playbackSeries = useMemo(
    () =>
      playbackCandles && playbackSymbol
        ? {
            realtime: playbackCandles.slice(0, playbackIndex),
            one_minute: playbackCandles.slice(0, playbackIndex),
            five_minute: [],
          }
      : null,
    [playbackCandles, playbackIndex, playbackSymbol],
  )
  const monitorPoints = useMemo(
    () =>
      realtimePointsForSignals(
        snapshot?.signals ?? [],
        snapshot?.chart_series.realtime ?? snapshot?.candles ?? [],
        selectedSymbol,
      ),
    [selectedSymbol, snapshot?.candles, snapshot?.chart_series.realtime, snapshot?.signals],
  )
  const monitorReplay = useMemo<AppReplayResult | null>(() => {
    if (!monitorEnabled) return null
    const date = last(monitorPoints)?.timestamp.slice(0, 10) ?? new Date().toISOString().slice(0, 10)
    return {
      date,
      mode: 'strict',
      strict: true,
      points: monitorPoints,
      summary: replaySummaryFromPoints(monitorPoints),
    }
  }, [monitorEnabled, monitorPoints])
  const latestRealtimeTimestamp = last(
    (snapshot?.chart_series.realtime ?? snapshot?.candles ?? []).filter(
      (candle) => candle.symbol === selectedSymbol,
    ),
  )?.timestamp
  const liveTradeDate = latestRealtimeTimestamp?.slice(0, 10) ?? null
  const selectedDayUsesLiveQuote =
    Boolean(selectedDayReplay) && selectedDayReplay?.date === liveTradeDate && !selectedDayPayload?.quote
  const usesLiveSnapshot =
    !playbackSeries &&
    !selectedRecentReplayDay &&
    (monitorEnabled || !selectedDayReplay || selectedDayUsesLiveQuote)
  const visibleChartSeries =
    playbackSeries ??
    (monitorEnabled
      ? snapshot?.chart_series
      : selectedRecentReplayDay?.chart_series ?? selectedDayReplay?.chart_series ?? snapshot?.chart_series)
  const chartData = useMemo(
    () =>
      buildChartPoints(
        (visibleChartSeries ? chartCandles(visibleChartSeries, chartMode) : []).filter(
          (candle) => candle.symbol === selectedSymbol,
        ),
      ),
    [chartMode, selectedSymbol, visibleChartSeries],
  )
  const realtimeChartData = useMemo(
    () =>
      buildChartPoints(
        (visibleChartSeries?.realtime ?? snapshot?.candles ?? []).filter(
          (candle) => candle.symbol === selectedSymbol,
        ),
      ),
    [selectedSymbol, snapshot?.candles, visibleChartSeries],
  )
  const quoteSummary = useMemo(
    () =>
      quoteSummaryFor(
        quoteForVisibleContext({
          selectedDayQuote: selectedDayPayload?.quote,
          snapshotQuote: snapshot?.quotes?.[selectedSymbol],
          hasSelectedDay: Boolean(selectedDayReplay),
          hasRecentReplay: Boolean(selectedRecentReplayDay),
          hasPlayback: Boolean(playbackSeries),
          monitoring: monitorEnabled,
          usesLiveSnapshot,
        }),
        realtimeChartData,
      ),
    [
      playbackSeries,
      realtimeChartData,
      selectedDayPayload?.quote,
      selectedDayReplay,
      selectedRecentReplayDay,
      selectedSymbol,
      monitorEnabled,
      usesLiveSnapshot,
      snapshot?.quotes,
    ],
  )
  const chartReferencePrice = useMemo(
    () => quoteSummary?.reference ?? buildQuoteSummary(chartData)?.reference ?? null,
    [chartData, quoteSummary?.reference],
  )
  const visibleTradeDate = chartTradeDateLabel({
    monitorEnabled,
    selectedTradeDate,
    latestRealtimeTimestamp,
  })
  const selectedReplayPoints = useMemo(
    () =>
      (playbackStatus === 'idle' ? (replay?.points ?? []) : playbackPoints).filter(
        (point) => point.symbol === selectedSymbol,
      ),
    [playbackPoints, playbackStatus, replay?.points, selectedSymbol],
  )
  const visibleSignalPoints = monitorEnabled && playbackStatus === 'idle' ? monitorPoints : selectedReplayPoints
  const selectedReplayPoint = useMemo(
    () =>
      visibleSignalPoints.find((point) => replayPointKey(point) === selectedReplayKey) ??
      visibleSignalPoints.at(-1) ??
      null,
    [selectedReplayKey, visibleSignalPoints],
  )
  const chartMarkers = useMemo(
    () =>
      buildReplayMarkers(
        visibleSignalPoints.map((point) => ({
          symbol: point.symbol,
          timestamp: markerTimestampFor(point.timestamp, chartMode),
          action: point.action,
          price: point.price,
          confidence: point.confidence,
          llmAction: point.llm_action,
          llmConfidence: point.llm_confidence,
        })),
        chartData,
      ),
    [chartData, chartMode, visibleSignalPoints],
  )

  useEffect(() => {
    if (playbackStatus !== 'playing' || !playbackCandles || !playbackSymbol || !playbackDate) return
    const currentCandle = playbackCandles[playbackIndex - 1]
    const nextPoint = playbackQueue[0]
    if (currentCandle && nextPoint?.timestamp <= currentCandle.timestamp) {
      const timer = window.setTimeout(() => {
        setPlaybackStatus('reviewing')
        void reviewDayReplayPoint(playbackSymbol, playbackDate, nextPoint.timestamp)
          .then((reviewedPoint) => {
            setPlaybackPoints((current) => [...current, reviewedPoint])
            setReplay((current) =>
              current
                ? {
                    ...current,
                    points: [...current.points, reviewedPoint],
                    summary: {
                      ...current.summary,
                      reviewed_count: current.summary.reviewed_count + (reviewedPoint.llm_status === 'ok' ? 1 : 0),
                    },
                  }
                : current,
            )
            setSelectedReplayKey(replayPointKey(reviewedPoint))
            setPlaybackQueue((current) => current.slice(1))
          })
          .catch((err: unknown) => {
            setError(err instanceof Error ? err.message : '单点AI复核失败')
            setPlaybackPoints((current) => [...current, nextPoint])
            setPlaybackQueue((current) => current.slice(1))
          })
          .finally(() => setPlaybackStatus('playing'))
      }, 0)
      return () => window.clearTimeout(timer)
    }

    if (playbackIndex >= playbackCandles.length) {
      const timer = window.setTimeout(() => setPlaybackStatus('done'), 0)
      return () => window.clearTimeout(timer)
    }

    const timer = window.setTimeout(() => {
      setPlaybackIndex((current) => Math.min(current + 1, playbackCandles.length))
    }, 34)
    return () => window.clearTimeout(timer)
  }, [playbackCandles, playbackDate, playbackIndex, playbackQueue, playbackStatus, playbackSymbol])

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">A 股做 T 助手</p>
          <h1>1 分钟驱动，5 分钟确认</h1>
        </div>
        <div className="topbar-actions">
          <button
            type="button"
            className={`monitor-toggle ${currentMonitorStatus}`}
            aria-pressed={monitorEnabled}
            onClick={() => {
              setMonitorEnabled((current) => !current)
              setChartMode('realtime')
              setHoveredPoint(null)
            }}
          >
            <Pulse size={18} />
            <span>盯盘</span>
            <strong>{monitorStatusLabel(currentMonitorStatus)}</strong>
          </button>
          <button
            type="button"
            className="icon-button"
            onClick={() => void runSelectedDaySymbolReview()}
            disabled={isReplaying || playbackStatus === 'playing' || playbackStatus === 'reviewing'}
          >
            <Pulse size={18} />
            {replayReviewLoading || playbackStatus === 'reviewing' ? '复核中' : '复核所选日'}
          </button>
        </div>
      </header>

      {error && (
        <div className="notice error">
          <WarningCircle size={18} />
          行情/服务提示：{error}
        </div>
      )}

      {loading ? (
        <div className="loading-grid">
          <div />
          <div />
          <div />
        </div>
      ) : snapshot ? (
        <section className="workspace">
          <aside className="watchlist panel">
            <PanelTitle icon={<Database size={18} />} title="股票池" />
            <div className="watch-items">
              {snapshot.watchlist.map((item) => (
                <button
                  type="button"
                  key={item.symbol}
                  className={item.symbol === selectedSymbol ? 'watch-item active' : 'watch-item'}
                  onClick={() => {
                    setHoveredPoint(null)
                    setSelectedSymbol(item.symbol)
                    lastLoadedDaysSymbolRef.current = null
                    if (selectedTradeDate) {
                      void loadTradingDays(item.symbol, selectedTradeDate)
                    }
                  }}
                >
                  <span>
                    <strong>{item.symbol}</strong>
                    <small>{item.name}</small>
                  </span>
                  <StatusPill signal={latestSignalFor(snapshot.signals, item.symbol)} />
                </button>
              ))}
            </div>
            <div className="health-card">
              <span>行情源</span>
              <strong>{providerLabel(snapshot.provider_health.provider)}</strong>
              <small>
                更新 {formatTime(snapshot.provider_health.last_success_at)}，
                延迟 {snapshot.provider_health.latency_ms ?? 0}ms
              </small>
              <ProviderNote health={snapshot.provider_health} />
            </div>
          </aside>

          <section className="chart-panel panel">
            <div className="panel-title-row">
              <PanelTitle icon={<ChartLineUp size={18} />} title="分时与信号" />
              <div className="chart-tools">
                <TradeDateControl
                  value={visibleTradeDate}
                  reviewValue={selectedTradeDate}
                  monitoring={monitorEnabled}
                  days={tradingDays}
                  loading={dayLoading}
                  onChange={(date) => void changeTradeDate(date)}
                  onPrev={() => void changeTradeDate(shiftCalendarDate(selectedTradeDate, -1))}
                  onNext={() => void changeTradeDate(shiftCalendarDate(selectedTradeDate, 1))}
                />
                <ChartModeTabs
                  value={chartMode}
                  onChange={(mode) => {
                    setHoveredPoint(null)
                    setChartMode(mode)
                  }}
                />
              </div>
            </div>
            <QuoteHeader summary={quoteSummary} />
            <div className="chart-wrap">
              <PriceChart
                data={chartData}
                mode={chartMode}
                referencePrice={chartReferencePrice}
                markers={chartMarkers}
                onHoverPoint={setHoveredPoint}
              />
            </div>
            <ReplayStrip
              replay={replay}
              monitorReplay={monitorReplay}
              recentReplay={recentReplay}
              monitoring={monitorEnabled}
              monitorStatus={currentMonitorStatus}
              monitorLastPulledAt={monitorLastPulledAt}
              playbackStatus={playbackStatus}
              playbackTime={last(playbackSeries?.realtime ?? [])?.timestamp ?? null}
              selectedDate={selectedReplayDate}
              points={visibleSignalPoints}
              markers={chartMarkers}
              selectedKey={selectedReplayPoint ? replayPointKey(selectedReplayPoint) : null}
              onSelectDate={(date) => {
                const day = recentReplay?.days.find((item) => item.date === date)
                setHoveredPoint(null)
                setSelectedReplayDate(day?.date ?? null)
                setReplay(replayResultForDay(day))
                const nextReplayPoint = day?.points.find((point) => point.symbol === selectedSymbol)
                setSelectedReplayKey(nextReplayPoint ? replayPointKey(nextReplayPoint) : null)
                setPlaybackStatus('idle')
                setPlaybackCandles(null)
                setPlaybackDate(null)
                setPlaybackPoints([])
                setPlaybackQueue([])
              }}
              onSelect={(point) => setSelectedReplayKey(replayPointKey(point))}
            />
            <IndicatorStrip point={hoveredPoint ?? last(chartData)} mode={chartMode} />
            <div className="metric-row">
              <Metric label="最新价" value={last(chartData)?.close.toFixed(2) ?? '--'} />
              <Metric label="最近量" value={last(chartData)?.volume.toFixed(0) ?? '--'} />
              <Metric label="当前图表" value={chartModeLabel(chartMode)} />
              <Metric label="模型状态" value={selectedSignal?.llm_status ?? '--'} />
            </div>
            <SignalTimeline signals={snapshot.signals.filter((signal) => signal.symbol === selectedSymbol)} />
          </section>

          <aside className="decision-panel panel">
            <PanelTitle icon={<ClockCounterClockwise size={18} />} title="决策区" />
            <PositionCard position={selectedPosition} />
            <ReplayPointCard point={selectedReplayPoint} />
            <SignalCard signal={selectedSignal} />
            <div className="manual-actions">
              <button
                type="button"
                disabled={!selectedReplayPoint || tradeSavingAction !== null}
                onClick={() => void confirmSelectedTrade('buy')}
              >
                {tradeSavingAction === 'buy' ? '保存中' : '已低吸'}
              </button>
              <button
                type="button"
                disabled={!selectedReplayPoint || tradeSavingAction !== null}
                onClick={() => void confirmSelectedTrade('sell')}
              >
                {tradeSavingAction === 'sell' ? '保存中' : '已高抛'}
              </button>
              <button type="button">忽略</button>
            </div>
            <TradeStatsPanel
              stats={tradeStats}
              loading={tradeStatsLoading}
              error={tradeStatsError}
              deletingId={deletingTradeId}
              onDelete={(id) => void removeTradeConfirmation(id)}
            />
            <p className="risk-copy">仅作盘中观察与决策辅助，不自动下单，所有操作需要人工确认。</p>
          </aside>
        </section>
      ) : (
        <div className="empty-state">暂无快照数据</div>
      )}
    </main>
  )
}

function ReplayStrip({
  replay,
  monitorReplay,
  recentReplay,
  monitoring,
  monitorStatus,
  monitorLastPulledAt,
  playbackStatus,
  playbackTime,
  selectedDate,
  points,
  markers,
  selectedKey,
  onSelectDate,
  onSelect,
}: {
  replay: AppReplayResult | null
  monitorReplay: AppReplayResult | null
  recentReplay: RecentReplayResult | null
  monitoring: boolean
  monitorStatus: MonitorStatus
  monitorLastPulledAt: string | null
  playbackStatus: 'idle' | 'playing' | 'reviewing' | 'done'
  playbackTime: string | null
  selectedDate: string | null
  points: ReplayPoint[]
  markers: ChartMarker[]
  selectedKey: string | null
  onSelectDate: (date: string) => void
  onSelect: (point: ReplayPoint) => void
}) {
  const activeReplay = monitorReplay ?? replay
  if (!activeReplay) {
    return <div className="replay-strip muted">尚未执行今日回放</div>
  }

  return (
    <div className="replay-strip">
      <div className="replay-summary">
        <span>{activeReplay.date}</span>
        <strong>{points.length} 个点位</strong>
        <small>
          {replayModeLabel(activeReplay.mode)}，{replaySourceLabel({
            hasRecentReplay: Boolean(recentReplay),
            recentReviewEnabled: recentReplay?.review_enabled,
            monitoring,
            playbackActive: playbackStatus !== 'idle',
          })}，原始{' '}
          {activeReplay.summary.candidate_count}，已复核 {activeReplay.summary.reviewed_count}，图上 {markers.length}
        </small>
      </div>
      {monitoring && (
        <div className={`monitor-status ${monitorStatus}`}>
          <span>{monitorStripLabel(monitorStatus)}</span>
          <strong>{formatTime(monitorLastPulledAt)}</strong>
        </div>
      )}
      {playbackStatus !== 'idle' && (
        <div className={`playback-status ${playbackStatus}`}>
          <span>{playbackStatusLabel(playbackStatus)}</span>
          <strong>{formatTime(playbackTime)}</strong>
        </div>
      )}
      {recentReplay && (
        <>
          <ReplayDateTabs replay={recentReplay} selectedDate={selectedDate} onSelect={onSelectDate} />
          <RecentReplaySummary replay={recentReplay} />
        </>
      )}
      <div className="replay-points">
        {points.map((point) => (
          <button
            type="button"
            key={replayPointKey(point)}
            className={`replay-point ${replayPointTone(point)} ${
              selectedKey === replayPointKey(point) ? 'active' : ''
            }`}
            style={replayPointStyle(point)}
            onClick={() => onSelect(point)}
          >
            {formatTime(point.timestamp)} {actionLabel(point.llm_action ?? point.action)} {formatNullable(point.price)}
            <em>{replayPointReviewLabel(point)}</em>
          </button>
        ))}
      </div>
      {activeReplay.artifact_path && <small className="artifact-path">JSON {activeReplay.artifact_path}</small>}
    </div>
  )
}

function ReplayDateTabs({
  replay,
  selectedDate,
  onSelect,
}: {
  replay: RecentReplayResult
  selectedDate: string | null
  onSelect: (date: string) => void
}) {
  return (
    <div className="replay-date-tabs" role="tablist" aria-label="五日回放日期">
      {replay.days.map((day) => (
        <button
          type="button"
          key={day.date}
          className={selectedDate === day.date ? 'active' : ''}
          onClick={() => onSelect(day.date)}
        >
          <CalendarBlank size={14} />
          <span>{formatDateShort(day.date)}</span>
          <strong>{day.points.length}</strong>
          <em>{formatPercentValue(day.summary.accuracy_rate_pct)}</em>
        </button>
      ))}
    </div>
  )
}

function TradeDateControl({
  value,
  reviewValue,
  monitoring,
  days,
  loading,
  onChange,
  onPrev,
  onNext,
}: {
  value: string
  reviewValue: string
  monitoring: boolean
  days: string[]
  loading: boolean
  onChange: (date: string) => void
  onPrev: () => void
  onNext: () => void
}) {
  const [open, setOpen] = useState(false)
  const selectedDate = useMemo(() => isoDateToLocalDate(value), [value])
  const cachedDates = useMemo(() => new Set(days), [days])

  return (
    <div className="trade-date-control" aria-label="交易日选择">
      <button type="button" className="date-nav-button" onClick={onPrev} disabled={loading || !value}>
        <CaretLeft size={16} />
      </button>
      <div className="date-picker-popover">
        <button
          type="button"
          className="date-picker-trigger"
          onClick={() => setOpen((current) => !current)}
          disabled={loading}
          aria-expanded={open}
        >
          <CalendarBlank size={15} />
          <span>{value || '选择日期'}</span>
        </button>
        {monitoring && reviewValue && reviewValue !== value && (
          <small className="date-review-hint">复核 {reviewValue}</small>
        )}
        {open && (
          <div className="date-picker-panel">
            <DayPicker
              mode="single"
              locale={zhCN}
              weekStartsOn={1}
              selected={selectedDate ?? undefined}
              defaultMonth={selectedDate ?? undefined}
              onSelect={(date) => {
                if (!date) return
                setOpen(false)
                onChange(localDateToIsoDate(date))
              }}
              modifiers={{
                cached: (date) => cachedDates.has(localDateToIsoDate(date)),
              }}
              modifiersClassNames={{ cached: 'rdp-cached-day' }}
            />
          </div>
        )}
      </div>
      <button
        type="button"
        className="date-nav-button"
        onClick={onNext}
        disabled={loading || !value}
      >
        <CaretRight size={16} />
      </button>
    </div>
  )
}

function RecentReplaySummary({ replay }: { replay: RecentReplayResult }) {
  return (
    <div className="recent-summary">
      <Metric label="交易日" value={String(replay.summary.trading_day_count ?? replay.days.length)} />
      <Metric label="AI低吸" value={String(replay.summary.ai_buy_count ?? 0)} />
      <Metric label="AI高抛" value={String(replay.summary.ai_sell_count ?? 0)} />
      <Metric label="命中率" value={formatPercentValue(replay.summary.accuracy_rate_pct)} />
    </div>
  )
}

function ReplayPointCard({ point }: { point: ReplayPoint | null }) {
  if (!point) return <div className="empty-state">选择今日回放点后查看复核理由</div>

  const executionAllowed = point.execution_allowed
  const executionLabel =
    executionAllowed === true ? '可执行' : executionAllowed === false ? '不可直接执行' : '未AI复核'

  return (
    <div className={`replay-decision-card ${point.llm_action ?? point.action}`}>
      <div className="signal-head">
        <span>{formatTime(point.timestamp)} · {formatNullable(point.price)}</span>
        <strong>{actionLabel(point.llm_action ?? point.action)}</strong>
      </div>
      <div className="execution-row">
        <span>行情候选 {actionLabel(point.action)}</span>
        <em className={executionAllowed === false ? 'blocked' : 'allowed'}>{executionLabel}</em>
      </div>
      <p>{point.llm_summary ?? point.reason}</p>
      <ReviewList title="规则理由" items={[point.reason, ...point.rule_ids]} limit={4} />
      <ReviewList title="AI行情理由" items={point.llm_reasons} limit={4} />
      <ReviewList title="等待确认" items={point.wait_for} limit={4} />
      <ReviewList title="执行阻断" items={point.execution_blockers ?? []} limit={4} />
      {point.risks.length > 0 && <ReviewList title="风险" items={point.risks} limit={4} />}
    </div>
  )
}

function PanelTitle({ icon, title }: { icon: React.ReactNode; title: string }) {
  return (
    <div className="panel-title">
      {icon}
      <span>{title}</span>
    </div>
  )
}

function StatusPill({ signal }: { signal?: Signal }) {
  const kind = signal?.kind ?? 'hold'
  const labelMap = {
    candidate_buy: '低吸候选',
    candidate_sell: '高抛候选',
    suspected: '疑似点',
    hold: '观望',
  }
  return <em className={`status ${kind}`}>{labelMap[kind]}</em>
}

function PositionCard({ position }: { position?: Position }) {
  if (!position) return <div className="empty-state">未录入持仓</div>
  return (
    <div className="position-card">
      <div>
        <span>底仓</span>
        <strong>{position.base_quantity}</strong>
      </div>
      <div>
        <span>成本</span>
        <strong>{position.cost_price.toFixed(2)}</strong>
      </div>
      <div>
        <span>可用资金</span>
        <strong>{position.available_cash.toFixed(0)}</strong>
      </div>
      <div>
        <span>计划做T</span>
        <strong>{position.t_quantity}</strong>
      </div>
    </div>
  )
}

function SignalCard({ signal }: { signal?: Signal }) {
  if (!signal) return <div className="empty-state">暂无信号</div>
  return (
    <div className={`signal-card ${signal.action}`}>
      <div className="signal-head">
        <span>{formatTime(signal.timestamp)}</span>
        <strong>{signal.action === 'buy' ? '低吸' : signal.action === 'sell' ? '高抛' : '观望'}</strong>
      </div>
      <p>{signal.reason}</p>
      <div className="rule-list">
        {signal.rule_ids.length ? signal.rule_ids.map((rule) => <code key={rule}>{rule}</code>) : <code>hold</code>}
      </div>
      <ModelReview review={signal.llm_review} status={signal.llm_status} />
      {signal.risks.length > 0 && (
        <ul>
          {signal.risks.map((risk) => (
            <li key={risk}>{risk}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function ModelReview({ review, status }: { review?: LlmReview | null; status: string }) {
  if (!review) {
    return (
      <div className="model-review compact">
        <span>模型</span>
        <strong>{modelStatusLabel(status)}</strong>
      </div>
    )
  }

  return (
    <div className="model-review">
      <div className="model-review-head">
        <span>模型复核</span>
        <strong>{modelActionLabel(review.action)} · {(review.confidence * 100).toFixed(0)}%</strong>
      </div>
      <p>{review.summary}</p>
      <div className="review-columns">
        <ReviewList title="理由" items={review.reasons} />
        <ReviewList title="等待" items={review.wait_for} />
      </div>
      <ExecutionReview review={review} />
      {review.risks.length > 0 && <ReviewList title="风险" items={review.risks} />}
    </div>
  )
}

function ExecutionReview({ review }: { review: LlmReview }) {
  const blockers = review.execution_blockers ?? []
  if (review.execution_allowed === undefined && !blockers.length) return null
  return (
    <ReviewList
      title={review.execution_allowed === false ? '执行阻断' : '执行条件'}
      items={blockers.length ? blockers : ['账户与交易条件未形成阻断']}
    />
  )
}

function ReviewList({ title, items, limit = 3 }: { title: string; items: string[]; limit?: number }) {
  if (!items.length) return null
  return (
    <div className="review-list">
      <span>{title}</span>
      {items.slice(0, limit).map((item) => (
        <small key={item}>{item}</small>
      ))}
    </div>
  )
}

function SignalTimeline({ signals }: { signals: Signal[] }) {
  return (
    <div className="timeline">
      {signals.slice(-5).map((signal) => (
        <div key={`${signal.timestamp}-${signal.kind}`}>
          <span>{formatTime(signal.timestamp)}</span>
          <strong>{signal.kind}</strong>
          <small>{signal.reason}</small>
        </div>
      ))}
    </div>
  )
}

function TradeStatsPanel({
  stats,
  loading,
  error,
  deletingId,
  onDelete,
}: {
  stats: TradeConfirmationStats | null
  loading: boolean
  error: string
  deletingId: string | null
  onDelete: (id: string) => void
}) {
  return (
    <section className="trade-stats">
      <div className="trade-stats-head">
        <div>
          <span>做T统计</span>
          <strong>{stats?.date ?? '--'}</strong>
        </div>
        {loading && <small>刷新中</small>}
      </div>
      {error && <p className="trade-stats-error">{error}</p>}
      <div className="trade-stats-metrics">
        <Metric label="差价收益" value={formatTradeMoney(stats?.summary.total_pnl ?? 0)} />
        <Metric label="已配对" value={String(stats?.summary.paired_count ?? 0)} />
        <Metric label="待配对" value={String(stats?.summary.unpaired_count ?? 0)} />
        <Metric label="记录" value={String(stats?.summary.record_count ?? 0)} />
      </div>
      {stats?.pairs.length ? (
        <div className="trade-pairs">
          {stats.pairs.slice(0, 4).map((pair) => (
            <div key={`${pair.buy_id}-${pair.sell_id}`} className="trade-pair-row">
              <span>{pair.symbol}</span>
              <strong>{formatTradeMoney(pair.pnl)}</strong>
              <small>
                {formatNullable(pair.buy_price)} → {formatNullable(pair.sell_price)}
              </small>
              <div className="trade-pair-actions">
                <button type="button" disabled={deletingId === pair.buy_id} onClick={() => onDelete(pair.buy_id)}>
                  删低吸
                </button>
                <button type="button" disabled={deletingId === pair.sell_id} onClick={() => onDelete(pair.sell_id)}>
                  删高抛
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="trade-empty">暂无配对记录</div>
      )}
      {stats?.unpaired.length ? (
        <div className="trade-unpaired">
          <span>待配对</span>
          {stats.unpaired.slice(0, 5).map((item) => (
            <div key={item.id} className="trade-unpaired-row">
              <strong>{tradeConfirmationActionLabel(item.confirm_action)}</strong>
              <small>
                {item.symbol} {formatTime(item.signal_timestamp)} {formatNullable(item.price)}
              </small>
              <button type="button" disabled={deletingId === item.id} onClick={() => onDelete(item.id)}>
                删除
              </button>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

function QuoteHeader({ summary }: { summary: QuoteSummary | null }) {
  if (!summary) return null
  const trendClass = summary.change > 0 ? 'up' : summary.change < 0 ? 'down' : 'flat'
  return (
    <div className={`quote-header ${trendClass}`}>
      <div className="quote-primary">
        <strong>{formatNullable(summary.latest)}</strong>
        <span>{formatSigned(summary.change)}</span>
        <span>{formatPercent(summary.changePercent)}</span>
      </div>
      <div className="quote-extremes">
        <QuoteItem label="高" value={summary.high} tone={summary.high >= summary.reference ? 'up' : 'down'} />
        <QuoteItem label="低" value={summary.low} tone={summary.low >= summary.reference ? 'up' : 'down'} />
        <QuoteItem label="开" value={summary.open} tone={summary.open >= summary.reference ? 'up' : 'down'} />
      </div>
    </div>
  )
}

function QuoteItem({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone: 'up' | 'down'
}) {
  return (
    <span className={`quote-item ${tone}`}>
      <small>{label}</small>
      <strong>{formatNullable(value)}</strong>
    </span>
  )
}

function PriceChart({
  data,
  mode,
  referencePrice,
  markers,
  onHoverPoint,
}: {
  data: ReturnType<typeof buildChartPoints>
  mode: ChartMode
  referencePrice: number | null
  markers: ChartMarker[]
  onHoverPoint: (point: ReturnType<typeof buildChartPoints>[number] | null) => void
}) {
  return (
    <FinancialChart
      data={data}
      mode={mode}
      referencePrice={referencePrice}
      markers={markers}
      onHoverPoint={onHoverPoint}
    />
  )
}

function IndicatorStrip({
  point,
  mode,
}: {
  point?: ReturnType<typeof buildChartPoints>[number]
  mode: ChartMode
}) {
  if (mode === 'realtime') {
    return (
      <div className="indicator-strip">
        <span className="avg-price">均价:{formatNullable(point?.avgPrice)}</span>
        <span className="latest-price">最新:{formatNullable(point?.close)}</span>
      </div>
    )
  }

  const trendClass = trendClassFor(point?.change ?? 0)
  return (
    <div className={`indicator-strip kline-strip ${trendClass}`}>
      <span className="k-time">{point?.time ?? '--:--'}</span>
      <span>开:{formatNullable(point?.open)}</span>
      <span>高:{formatNullable(point?.high)}</span>
      <span>低:{formatNullable(point?.low)}</span>
      <span>收:{formatNullable(point?.close)}</span>
      <span className="k-change">涨跌:{formatSignedNullable(point?.change)}</span>
      <span className="k-change-percent">涨跌幅:{formatPercentNullable(point?.changePercent)}</span>
      <span className="ma5">MA5 {formatNullable(point?.ma5)}</span>
      <span className="ma10">MA10 {formatNullable(point?.ma10)}</span>
      <span className="ma20">MA20 {formatNullable(point?.ma20)}</span>
    </div>
  )
}

function ChartModeTabs({
  value,
  onChange,
}: {
  value: ChartMode
  onChange: (value: ChartMode) => void
}) {
  const options: { value: ChartMode; label: string }[] = [
    { value: 'realtime', label: '实时' },
    { value: 'one_minute', label: '1分钟K' },
    { value: 'five_minute', label: '5分钟K' },
  ]
  return (
    <div className="segmented" role="tablist" aria-label="图表周期">
      {options.map((option) => (
        <button
          type="button"
          key={option.value}
          className={value === option.value ? 'active' : ''}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

function ProviderNote({ health }: { health: ProviderHealth }) {
  if (!health.provider.includes('fallback')) return null
  return (
    <small className="fallback-note">
      真实行情源暂不可用，当前显示离线模拟数据
      {health.last_error ? `：${health.last_error}` : ''}
    </small>
  )
}

function latestSignalFor(signals: Signal[], symbol: string) {
  return signals.filter((signal) => signal.symbol === symbol).at(-1)
}

function replayPointKey(point: Pick<ReplayPoint, 'symbol' | 'timestamp' | 'action'>) {
  return `${point.symbol}-${point.timestamp}-${point.action}`
}

function replayPointTone(point: ReplayPoint) {
  return point.llm_action ?? point.action
}

function replayPointStyle(point: ReplayPoint) {
  const color = markerColor(replayPointTone(point), point.llm_confidence ?? point.confidence)
  return {
    borderColor: color,
    color,
  }
}

function realtimePointsForSignals(signals: Signal[], candles: Candle[], symbol: string): ReplayPoint[] {
  const prices = new Map(
    candles
      .filter((candle) => candle.symbol === symbol)
      .map((candle) => [candle.timestamp, candle.close]),
  )
  const points: ReplayPoint[] = []
  signals
    .filter((signal) => signal.symbol === symbol && signal.kind !== 'hold')
    .forEach((signal) => {
      const price = prices.get(signal.timestamp)
      if (typeof price !== 'number') return
      const review = signal.llm_review
      points.push({
        symbol: signal.symbol,
        timestamp: signal.timestamp,
        action: signal.action,
        kind: signal.kind,
        price,
        confidence: signal.confidence,
        rule_ids: signal.rule_ids,
        reason: signal.reason,
        risks: signal.risks,
        llm_status: signal.llm_status,
        llm_action: review?.action ?? null,
        llm_confidence: review?.confidence ?? null,
        llm_summary: review?.summary ?? null,
        llm_reasons: review?.reasons ?? [],
        wait_for: review?.wait_for ?? [],
        execution_allowed: review?.execution_allowed ?? null,
        execution_blockers: review?.execution_blockers ?? [],
      })
    })
  return points
}

function last<T>(items: T[]): T | undefined {
  return items.at(-1)
}

function chartCandles(chartSeries: Snapshot['chart_series'], mode: ChartMode) {
  return chartSeries[mode]
}

function chartModeLabel(mode: ChartMode) {
  if (mode === 'realtime') return '实时'
  if (mode === 'one_minute') return '1分钟K'
  return '5分钟K'
}

function replayModeLabel(mode: ReplayResult['mode']) {
  return mode === 'strict' ? '严格回放' : '择优分析'
}

function playbackStatusLabel(status: 'idle' | 'playing' | 'reviewing' | 'done') {
  if (status === 'playing') return '逐分钟推进'
  if (status === 'reviewing') return '等待AI复核'
  if (status === 'done') return '复核完成'
  return '未开始'
}

function monitorStatusLabel(status: MonitorStatus) {
  if (status === 'active') return '开盘中'
  if (status === 'paused') return '休市'
  return '关闭'
}

function monitorStripLabel(status: MonitorStatus) {
  if (status === 'active') return '实时 AI 复核'
  if (status === 'paused') return '等待开盘'
  return '盯盘关闭'
}

function quoteSummaryFor(quote: MarketQuote | undefined, fallbackPoints: ReturnType<typeof buildChartPoints>) {
  if (quote) {
    return {
      latest: quote.latest,
      change: quote.change,
      changePercent: quote.change_percent,
      open: quote.open,
      high: quote.high,
      low: quote.low,
      reference: quote.previous_close,
    } satisfies QuoteSummary
  }
  return buildQuoteSummary(fallbackPoints)
}

function formatNullable(value?: number | null) {
  return typeof value === 'number' ? value.toFixed(2) : '--'
}

function formatSignedNullable(value?: number | null) {
  return typeof value === 'number' ? formatSigned(value) : '--'
}

function formatPercentNullable(value?: number | null) {
  return typeof value === 'number' ? formatPercent(value) : '--'
}

function formatPercentValue(value?: number | null) {
  return typeof value === 'number' ? `${value.toFixed(2)}%` : '--'
}

function formatSigned(value: number) {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}`
}

function formatPercent(value: number) {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}

function actionLabel(action: ReplayPoint['action']) {
  if (action === 'buy') return '低吸'
  if (action === 'sell') return '高抛'
  return '观望'
}

function markerTimestampFor(timestamp: string, mode: ChartMode) {
  if (mode !== 'five_minute') return timestamp
  const date = timestamp.slice(0, 10)
  const [hour, minute] = timestamp.slice(11, 16).split(':').map(Number)
  const total = hour * 60 + minute
  const sessionOpen = total <= 11 * 60 + 30 ? 9 * 60 + 30 : 13 * 60
  const elapsed = Math.max(1, total - sessionOpen)
  const bucketClose = sessionOpen + Math.ceil(elapsed / 5) * 5
  const closeHour = Math.floor(bucketClose / 60)
  const closeMinute = bucketClose % 60
  return `${date}T${String(closeHour).padStart(2, '0')}:${String(closeMinute).padStart(2, '0')}:00`
}

function trendClassFor(value: number) {
  if (value > 0) return 'up'
  if (value < 0) return 'down'
  return 'flat'
}

function modelStatusLabel(status: string) {
  if (status === 'ok') return '已复核'
  if (status === 'pending') return 'AI复核中'
  if (status === 'failed') return '复核失败'
  if (status === 'not_requested') return '未触发'
  return status || '--'
}

function modelActionLabel(action: LlmReview['action']) {
  if (action === 'buy') return '低吸'
  if (action === 'sell') return '高抛'
  return '观望'
}

function formatTime(value: string | null) {
  if (!value) return '--:--'
  return value.slice(11, 16)
}

function formatDateShort(value: string) {
  return value.slice(5)
}

function isoDateToLocalDate(value: string) {
  const [year, month, day] = value.split('-').map(Number)
  if (!year || !month || !day) return null
  const date = new Date(year, month - 1, day)
  return Number.isNaN(date.getTime()) ? null : date
}

function localDateToIsoDate(value: Date) {
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function replayResultForDay(day?: RecentReplayDay | null): AppReplayResult | null {
  return day
    ? {
        date: day.date,
        mode: day.mode,
        strict: day.strict,
        points: day.points,
        summary: day.summary,
      }
    : null
}

function providerLabel(provider: string) {
  if (provider === 'tencent_ifzq') return '腾讯真实分时'
  if (provider === 'tencent_ifzq_fallback') return '腾讯分时回退'
  if (provider === 'akshare') return 'AKShare 真实行情'
  if (provider === 'akshare_fallback') return 'AKShare 回退模式'
  return provider
}

export default App
