export type ChartCandle = {
  timestamp: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export type ChartPoint = {
  timestamp: string
  time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  avgPrice: number | null
  change: number
  changePercent: number
  changeReference: number
  ma5: number | null
  ma10: number | null
  ma20: number | null
}

export type PercentScalePoint = {
  timestamp: string
  time: string
  open: number
  high: number
  low: number
  close: number
  avgPrice: number | null
  ma5: number | null
  ma10: number | null
  ma20: number | null
}

export type PercentAxisTick = {
  value: number
  label: string
  position: number
}

export type PercentAxis = {
  min: number
  max: number
  ticks: PercentAxisTick[]
}

export type QuoteSummary = {
  latest: number
  change: number
  changePercent: number
  open: number
  high: number
  low: number
  reference: number
}

export type ReplayAction = 'buy' | 'sell' | 'hold'

export type ReplayMarkerPoint = {
  symbol: string
  timestamp: string
  action: ReplayAction
  price: number
  confidence: number
  llmAction?: ReplayAction | null
  llmConfidence?: number | null
}

export type ChartMarker = {
  id: string
  timestamp: string
  time: string
  action: ReplayAction
  price: number
  confidence: number
  llmAction?: ReplayAction | null
  llmConfidence?: number | null
  label: string
}

export type TradingDayTimeAnchor = {
  timestamp: string
  time: string
}

export function buildChartPoints(candles: ChartCandle[]): ChartPoint[] {
  let currentDate = ''
  let cumulativeAmount = 0
  let cumulativeVolume = 0
  let cumulativeClose = 0
  let cumulativeCount = 0

  return candles.map((candle, index) => ({
    ...buildPoint(candle, index),
  }))

  function buildPoint(candle: ChartCandle, index: number): ChartPoint {
    const date = candle.timestamp.slice(0, 10)
    if (date !== currentDate) {
      currentDate = date
      cumulativeAmount = 0
      cumulativeVolume = 0
      cumulativeClose = 0
      cumulativeCount = 0
    }

    cumulativeAmount += candle.close * candle.volume
    cumulativeVolume += candle.volume
    cumulativeClose += candle.close
    cumulativeCount += 1
    const changeReference = candles[index - 1]?.close ?? candle.open
    const change = candle.close - changeReference

    return {
      timestamp: candle.timestamp,
      time: candle.timestamp.slice(11, 16),
      open: candle.open,
      high: candle.high,
      low: candle.low,
      close: candle.close,
      volume: candle.volume,
      avgPrice: cumulativeVolume > 0 ? cumulativeAmount / cumulativeVolume : cumulativeClose / cumulativeCount,
      change,
      changePercent: changeReference > 0 ? (change / changeReference) * 100 : 0,
      changeReference,
      ma5: movingAverage(candles, index, 5),
      ma10: movingAverage(candles, index, 10),
      ma20: movingAverage(candles, index, 20),
    }
  }
}

export function buildTradingDayTimeAnchors(tradeDate: string): TradingDayTimeAnchor[] {
  return [
    ...buildSessionTimeAnchors(tradeDate, '09:30', '11:30'),
    ...buildSessionTimeAnchors(tradeDate, '13:00', '15:00'),
  ]
}

export function fixedTimeAxisInteractionOptions() {
  return {
    handleScroll: {
      mouseWheel: false,
      pressedMouseMove: false,
      horzTouchDrag: false,
      vertTouchDrag: false,
    },
    handleScale: {
      mouseWheel: false,
      pinch: false,
      axisPressedMouseMove: false,
      axisDoubleClickReset: false,
    },
    kineticScroll: {
      mouse: false,
      touch: false,
    },
  }
}

export function buildReplayMarkers(
  points: ReplayMarkerPoint[],
  visiblePoints: ChartPoint[],
): ChartMarker[] {
  const visibleTimes = new Set(visiblePoints.map((point) => point.timestamp))
  return points
    .filter((point) => visibleTimes.has(point.timestamp))
    .map((point) => {
      const time = point.timestamp.slice(11, 16)
      return {
        id: `${point.symbol}-${point.timestamp}-${point.action}`,
        timestamp: point.timestamp,
        time,
        action: point.action,
        price: point.price,
        confidence: point.confidence,
        llmAction: point.llmAction,
        llmConfidence: point.llmConfidence,
        label: point.action === 'buy' ? 'B' : point.action === 'sell' ? 'S' : 'H',
      }
    })
}

export function filterMarkersForSegment(markers: ChartMarker[], segment: Pick<ChartPoint, 'timestamp'>[]) {
  const timestamps = new Set(segment.map((point) => point.timestamp))
  return markers.filter((marker) => timestamps.has(marker.timestamp))
}

export function movingAverage(candles: ChartCandle[], index: number, period: number): number | null {
  const start = index - period + 1
  if (start < 0) return null
  const window = candles.slice(start, index + 1)
  const total = window.reduce((sum, candle) => sum + candle.close, 0)
  return total / period
}

export function buildPercentScalePoints(points: ChartPoint[], referencePrice?: number | null): PercentScalePoint[] {
  if (!referencePrice || referencePrice <= 0) return []

  return points.map((point) => ({
    timestamp: point.timestamp,
    time: point.time,
    open: percentChange(point.open, referencePrice),
    high: percentChange(point.high, referencePrice),
    low: percentChange(point.low, referencePrice),
    close: percentChange(point.close, referencePrice),
    avgPrice: point.avgPrice === null ? null : percentChange(point.avgPrice, referencePrice),
    ma5: point.ma5 === null ? null : percentChange(point.ma5, referencePrice),
    ma10: point.ma10 === null ? null : percentChange(point.ma10, referencePrice),
    ma20: point.ma20 === null ? null : percentChange(point.ma20, referencePrice),
  }))
}

export function buildPercentAxisTicks(
  points: ChartPoint[],
  referencePrice?: number | null,
  tickCount = 5,
): PercentAxis | null {
  const percentPoints = buildPercentScalePoints(points, referencePrice)
  if (!percentPoints.length) return null

  const values = percentPoints.flatMap((point) =>
    [point.open, point.high, point.low, point.close, point.avgPrice, point.ma5, point.ma10, point.ma20].filter(
      (value): value is number => typeof value === 'number' && Number.isFinite(value),
    ),
  )
  if (!values.length) return null

  const rawMin = Math.min(...values)
  const rawMax = Math.max(...values)
  const padding = Math.max((rawMax - rawMin) * 0.12, 0.05)
  const min = rawMin - padding
  const max = rawMax + padding
  const steps = Math.max(1, tickCount - 1)

  return {
    min,
    max,
    ticks: Array.from({ length: tickCount }, (_, index) => {
      const value = max - ((max - min) / steps) * index
      return {
        value,
        label: formatSignedPercent(value),
        position: ((max - value) / (max - min)) * 100,
      }
    }),
  }
}

export function buildQuoteSummary(points: ChartPoint[], previousClose?: number | null): QuoteSummary | null {
  const first = points[0]
  const latest = points.at(-1)
  if (!first || !latest) return null

  const reference = previousClose && previousClose > 0 ? previousClose : first.open
  const change = latest.close - reference
  const changePercent = reference > 0 ? (change / reference) * 100 : 0

  return {
    latest: latest.close,
    change,
    changePercent,
    open: first.open,
    high: Math.max(...points.map((point) => point.high)),
    low: Math.min(...points.map((point) => point.low)),
    reference,
  }
}

function percentChange(value: number, reference: number) {
  return ((value - reference) / reference) * 100
}

function formatSignedPercent(value: number) {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}

function buildSessionTimeAnchors(tradeDate: string, start: string, end: string): TradingDayTimeAnchor[] {
  const anchors: TradingDayTimeAnchor[] = []
  const startMinute = minuteOfDay(start)
  const endMinute = minuteOfDay(end)

  for (let minute = startMinute; minute <= endMinute; minute += 1) {
    const time = timeOfDay(minute)
    anchors.push({
      timestamp: `${tradeDate}T${time}:00`,
      time,
    })
  }

  return anchors
}

function minuteOfDay(time: string) {
  const [hour, minute] = time.split(':').map(Number)
  return hour * 60 + minute
}

function timeOfDay(minuteOfDayValue: number) {
  const hour = Math.floor(minuteOfDayValue / 60)
  const minute = minuteOfDayValue % 60
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
}
