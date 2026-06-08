import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildChartPoints,
  buildPercentAxisTicks,
  buildPercentScalePoints,
  buildQuoteSummary,
  buildReplayMarkers,
  markerColor,
  buildTradingDayTimeAnchors,
  fixedTimeAxisInteractionOptions,
  filterMarkersForSegment,
  type ChartCandle,
} from '../src/charting.ts'

test('buildChartPoints computes intraday volume weighted average price', () => {
  const candles: ChartCandle[] = [
    candle('2026-06-05T09:30:00', 10, 100),
    candle('2026-06-05T09:31:00', 12, 300),
    candle('2026-06-05T09:32:00', 11, 0),
  ]

  const points = buildChartPoints(candles)

  assert.deepEqual(
    points.map((point) => point.avgPrice),
    [10, 11.5, 11.5],
  )
})

test('buildChartPoints falls back to cumulative average before volume appears', () => {
  const candles: ChartCandle[] = [
    candle('2026-06-05T09:30:00', 10, 0),
    candle('2026-06-05T09:31:00', 12, 0),
  ]

  const points = buildChartPoints(candles)

  assert.deepEqual(
    points.map((point) => point.avgPrice),
    [10, 11],
  )
})

test('buildChartPoints calculates candle change against the previous close', () => {
  const points = buildChartPoints([
    candle('2026-06-05T09:30:00', 10, 100, { open: 9.8 }),
    candle('2026-06-05T09:31:00', 10.5, 300),
    candle('2026-06-05T09:32:00', 10.29, 200),
  ])

  assert.ok(closeTo(points[0].change, 0.2))
  assert.ok(closeTo(points[0].changePercent, 2.0408163265306145))
  assert.equal(points[0].changeReference, 9.8)
  assert.ok(closeTo(points[1].change, 0.5))
  assert.ok(closeTo(points[1].changePercent, 5))
  assert.equal(points[1].changeReference, 10)
  assert.ok(closeTo(points[2].change, -0.21))
  assert.ok(closeTo(points[2].changePercent, -2))
  assert.equal(points[2].changeReference, 10.5)
})

test('buildQuoteSummary calculates latest change open high and low from intraday points', () => {
  const points = buildChartPoints([
    candle('2026-06-05T09:30:00', 10, 100, { high: 10.2, low: 9.8 }),
    candle('2026-06-05T09:31:00', 10.5, 300, { high: 10.8, low: 10.1 }),
    candle('2026-06-05T09:32:00', 9.9, 200, { high: 10.1, low: 9.7 }),
  ])

  const summary = buildQuoteSummary(points)

  assert.equal(summary?.latest, 9.9)
  assert.ok(closeTo(summary?.change, -0.1))
  assert.ok(closeTo(summary?.changePercent, -1))
  assert.equal(summary?.open, 10)
  assert.equal(summary?.high, 10.8)
  assert.equal(summary?.low, 9.7)
  assert.equal(summary?.reference, 10)
})

test('buildQuoteSummary prefers previous close when it is available', () => {
  const points = buildChartPoints([
    candle('2026-06-05T09:30:00', 10, 100),
    candle('2026-06-05T09:31:00', 10.5, 300),
  ])

  const summary = buildQuoteSummary(points, 9.8)

  assert.ok(closeTo(summary?.change, 0.7))
  assert.ok(closeTo(summary?.changePercent, 7.142857142857151))
  assert.equal(summary?.reference, 9.8)
})

test('buildPercentScalePoints maps candle prices to previous-close percent change', () => {
  const points = buildChartPoints([
    candle('2026-06-05T09:30:00', 10, 100, { open: 9.8, high: 10.2, low: 9.7 }),
    candle('2026-06-05T09:31:00', 10.5, 300, { open: 10, high: 10.8, low: 9.9 }),
  ])

  const percentPoints = buildPercentScalePoints(points, 9.8)

  assert.equal(percentPoints.length, 2)
  assert.equal(percentPoints[0]?.timestamp, '2026-06-05T09:30:00')
  assert.equal(percentPoints[0]?.time, '09:30')
  assert.equal(percentPoints[0]?.open, 0)
  assert.ok(closeTo(percentPoints[0]?.high, 4.081632653061229))
  assert.ok(closeTo(percentPoints[0]?.low, -1.020408163265299))
  assert.ok(closeTo(percentPoints[0]?.close, 2.0408163265306145))
  assert.ok(closeTo(percentPoints[0]?.avgPrice ?? undefined, 2.0408163265306145))
  assert.ok(closeTo(percentPoints[1]?.close, 7.142857142857151))
})

test('buildPercentAxisTicks creates signed percent labels for chart axis overlay', () => {
  const points = buildChartPoints([
    candle('2026-06-05T09:30:00', 10, 100, { open: 9.8, high: 10.2, low: 9.7 }),
    candle('2026-06-05T09:31:00', 10.5, 300, { open: 10, high: 10.8, low: 9.9 }),
  ])

  const axis = buildPercentAxisTicks(points, 9.8)

  assert.equal(axis?.ticks.length, 5)
  assert.equal(axis?.ticks[0]?.label.startsWith('+'), true)
  assert.equal(axis?.ticks.at(-1)?.label.startsWith('-'), true)
  assert.ok(axis?.ticks.some((tick) => tick.label.includes('%')))
  assert.ok(axis?.ticks.every((tick) => tick.position >= 0 && tick.position <= 100))
  assert.ok(axis && axis.min < 0 && axis.max > 0)
})

test('buildReplayMarkers maps replay points only when the chart time is visible', () => {
  const points = buildChartPoints([
    candle('2026-06-05T10:40:00', 10, 100),
    candle('2026-06-05T10:41:00', 9.8, 300),
  ])

  const markers = buildReplayMarkers(
    [
      {
        symbol: '300502',
        timestamp: '2026-06-05T10:41:00',
        action: 'buy',
        price: 9.8,
        confidence: 0.86,
        llmAction: 'hold',
      },
      {
        symbol: '300502',
        timestamp: '2026-06-05T13:31:00',
        action: 'sell',
        price: 10.2,
        confidence: 0.62,
        llmAction: 'hold',
      },
    ],
    points,
  )

  assert.equal(markers.length, 1)
  assert.equal(markers[0]?.time, '10:41')
  assert.equal(markers[0]?.label, 'H86')
})

test('buildReplayMarkers uses AI action and confidence for marker labels and color depth', () => {
  const points = buildChartPoints([
    candle('2026-06-05T10:40:00', 10, 100),
    candle('2026-06-05T10:41:00', 9.8, 300),
    candle('2026-06-05T10:42:00', 10.2, 300),
  ])

  const markers = buildReplayMarkers(
    [
      {
        symbol: '300502',
        timestamp: '2026-06-05T10:41:00',
        action: 'buy',
        price: 9.8,
        confidence: 0.72,
        llmAction: 'buy',
        llmConfidence: 0.55,
      },
      {
        symbol: '300502',
        timestamp: '2026-06-05T10:42:00',
        action: 'buy',
        price: 10.2,
        confidence: 0.72,
        llmAction: 'sell',
        llmConfidence: 0.91,
      },
    ],
    points,
  )

  assert.equal(markers[0]?.action, 'buy')
  assert.equal(markers[0]?.label, 'B55')
  assert.equal(markers[0]?.color, '#dc7a7a')
  assert.equal(markers[1]?.action, 'sell')
  assert.equal(markers[1]?.label, 'S91')
  assert.equal(markers[1]?.color, '#16693c')
})

test('markerColor darkens buy red and sell green as AI confidence rises', () => {
  assert.equal(markerColor('buy', 0.52), '#dc7a7a')
  assert.equal(markerColor('buy', 0.92), '#a92222')
  assert.equal(markerColor('sell', 0.52), '#65b783')
  assert.equal(markerColor('sell', 0.92), '#16693c')
  assert.equal(markerColor('hold', 0.92), '#687570')
})

test('filterMarkersForSegment keeps replay markers on their owning line segment', () => {
  const points = buildChartPoints([
    candle('2026-06-05T09:46:00', 10, 100),
    candle('2026-06-05T09:54:00', 10.2, 120),
    candle('2026-06-05T13:01:00', 10.4, 160),
  ])
  const markers = buildReplayMarkers(
    [
      {
        symbol: '300502',
        timestamp: '2026-06-05T09:46:00',
        action: 'sell',
        price: 10,
        confidence: 0.72,
      },
      {
        symbol: '300502',
        timestamp: '2026-06-05T13:01:00',
        action: 'buy',
        price: 10.4,
        confidence: 0.72,
      },
    ],
    points,
  )

  const morning = filterMarkersForSegment(markers, points.slice(0, 2))
  const afternoon = filterMarkersForSegment(markers, points.slice(2))

  assert.deepEqual(
    morning.map((marker) => marker.time),
    ['09:46'],
  )
  assert.deepEqual(
    afternoon.map((marker) => marker.time),
    ['13:01'],
  )
})

test('buildTradingDayTimeAnchors returns the full A-share trading minute axis', () => {
  const anchors = buildTradingDayTimeAnchors('2026-06-05')

  assert.equal(anchors.length, 242)
  assert.equal(anchors[0]?.timestamp, '2026-06-05T09:30:00')
  assert.equal(anchors[120]?.timestamp, '2026-06-05T11:30:00')
  assert.equal(anchors[121]?.timestamp, '2026-06-05T13:00:00')
  assert.equal(anchors.at(-1)?.timestamp, '2026-06-05T15:00:00')
  assert.equal(anchors.some((anchor) => anchor.timestamp === '2026-06-05T11:31:00'), false)
  assert.equal(anchors.some((anchor) => anchor.timestamp === '2026-06-05T12:59:00'), false)
})

test('fixedTimeAxisInteractionOptions disables chart scroll and scale gestures', () => {
  const options = fixedTimeAxisInteractionOptions()

  assert.deepEqual(options.handleScroll, {
    mouseWheel: false,
    pressedMouseMove: false,
    horzTouchDrag: false,
    vertTouchDrag: false,
  })
  assert.deepEqual(options.handleScale, {
    mouseWheel: false,
    pinch: false,
    axisPressedMouseMove: false,
    axisDoubleClickReset: false,
  })
  assert.deepEqual(options.kineticScroll, {
    mouse: false,
    touch: false,
  })
})

function closeTo(actual: number | undefined, expected: number) {
  return typeof actual === 'number' && Math.abs(actual - expected) < 0.000001
}

function candle(
  timestamp: string,
  close: number,
  volume: number,
  overrides: Partial<Pick<ChartCandle, 'open' | 'high' | 'low'>> = {},
): ChartCandle {
  return {
    timestamp,
    open: overrides.open ?? close,
    high: overrides.high ?? close,
    low: overrides.low ?? close,
    close,
    volume,
  }
}
