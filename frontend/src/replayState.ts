export type ReplayPoint = {
  symbol: string
  timestamp: string
  action: 'buy' | 'sell' | 'hold'
  kind: 'candidate_buy' | 'candidate_sell' | 'suspected' | 'hold'
  price: number
  confidence: number
  rule_ids: string[]
  reason: string
  risks: string[]
  llm_status: string
  llm_action?: 'buy' | 'sell' | 'hold' | null
  llm_confidence?: number | null
  llm_summary?: string | null
  llm_reasons: string[]
  wait_for: string[]
  execution_allowed?: boolean | null
  execution_blockers?: string[]
}

export type ReplaySummary = {
  candidate_count: number
  buy_count: number
  sell_count: number
  reviewed_count: number
}

export type ReplayResult = {
  date: string
  mode: 'strict' | 'optimized'
  strict: boolean
  points: ReplayPoint[]
  summary: ReplaySummary
}

export type TradingDayPayload = {
  date: string
  points: ReplayPoint[]
  mode?: 'strict' | 'optimized'
  strict?: boolean
  summary?: ReplaySummary
}

export function dayMarketPayloadToReplay(payload: TradingDayPayload): ReplayResult {
  return {
    date: payload.date,
    mode: payload.mode ?? 'strict',
    strict: payload.strict ?? true,
    points: payload.points,
    summary: payload.summary ?? replaySummaryFromPoints(payload.points),
  }
}

export function replaySummaryFromPoints(points: ReplayPoint[]): ReplaySummary {
  return {
    candidate_count: points.length,
    buy_count: points.filter((point) => point.action === 'buy').length,
    sell_count: points.filter((point) => point.action === 'sell').length,
    reviewed_count: points.filter((point) => point.llm_status === 'ok').length,
  }
}

export function replaySourceLabel({
  hasRecentReplay,
  recentReviewEnabled = false,
  monitoring = false,
  playbackActive,
}: {
  hasRecentReplay: boolean
  recentReviewEnabled?: boolean
  monitoring?: boolean
  playbackActive: boolean
}) {
  if (monitoring) return '实时盯盘'
  if (playbackActive || (hasRecentReplay && recentReviewEnabled)) return 'AI复核'
  if (hasRecentReplay) return '快速回放'
  return '历史点位'
}

export function replayPointReviewLabel(point: Pick<ReplayPoint, 'llm_status' | 'llm_action'>) {
  if (point.llm_status === 'ok') return `AI${replayActionLabel(point.llm_action ?? 'hold')}`
  if (point.llm_status === 'failed') return 'AI失败'
  if (point.llm_status === 'pending') return 'AI复核中'
  if (point.llm_status === 'not_requested') return 'AI未触发'
  return 'AI未复核'
}

export function replayPointKey(point: Pick<ReplayPoint, 'symbol' | 'timestamp' | 'action'>) {
  return `${point.symbol}-${point.timestamp}-${point.action}`
}

export function selectedReplayPointForKey<T extends Pick<ReplayPoint, 'symbol' | 'timestamp' | 'action'>>(
  points: readonly T[],
  selectedKey: string | null,
) {
  if (!selectedKey) return null
  return points.find((point) => replayPointKey(point) === selectedKey) ?? null
}

export function replayActionLabel(action: ReplayPoint['action']) {
  if (action === 'buy') return '低吸'
  if (action === 'sell') return '高抛'
  return '观望'
}

export function shiftCalendarDate(value: string, offsetDays: number) {
  const [year, month, day] = value.split('-').map(Number)
  if (!year || !month || !day) return value
  const date = new Date(Date.UTC(year, month - 1, day))
  if (Number.isNaN(date.getTime())) return value
  date.setUTCDate(date.getUTCDate() + offsetDays)
  return date.toISOString().slice(0, 10)
}

export function chartTradeDateLabel({
  monitorEnabled,
  selectedTradeDate,
  latestRealtimeTimestamp,
}: {
  monitorEnabled: boolean
  selectedTradeDate: string
  latestRealtimeTimestamp?: string | null
}) {
  if (monitorEnabled && latestRealtimeTimestamp) return latestRealtimeTimestamp.slice(0, 10)
  return selectedTradeDate
}
