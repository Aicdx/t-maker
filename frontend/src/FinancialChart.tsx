import { useEffect, useRef } from 'react'
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  LineSeries,
  PriceScaleMode,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type LineSeriesPartialOptions,
  type MouseEventParams,
  type SeriesMarker,
  type SeriesType,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts'
import {
  buildPercentAxisTicks,
  buildPercentScalePoints,
  buildTradingDayTimeAnchors,
  fixedTimeAxisInteractionOptions,
  filterMarkersForSegment,
} from './charting'
import type { ChartMarker, ChartPoint, PercentAxis, PercentScalePoint } from './charting'

type ChartMode = 'realtime' | 'one_minute' | 'five_minute'
type PriceLikePoint = ChartPoint | PercentScalePoint

type FinancialChartProps = {
  data: ChartPoint[]
  mode: ChartMode
  referencePrice?: number | null
  markers?: ChartMarker[]
  onHoverPoint?: (point: ChartPoint | null) => void
}

type ChartSeries = ISeriesApi<SeriesType, Time>

const PERCENT_PRICE_SCALE_ID = 'left'

export function FinancialChart({ data, mode, referencePrice, markers = [], onHoverPoint }: FinancialChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const resizeObserverRef = useRef<ResizeObserver | null>(null)
  const seriesRefs = useRef<ChartSeries[]>([])
  const modeRef = useRef<ChartMode>(mode)
  const dataRef = useRef<ChartPoint[]>(data)
  const onHoverPointRef = useRef<((point: ChartPoint | null) => void) | undefined>(onHoverPoint)
  const percentAxis = buildPercentAxisTicks(data, referencePrice)

  useEffect(() => {
    modeRef.current = mode
    dataRef.current = data
    onHoverPointRef.current = onHoverPoint
  }, [data, mode, onHoverPoint])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const chart = createChart(container, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: '#fbfdfc' },
        textColor: '#687570',
        fontFamily: "Inter, 'Microsoft YaHei', system-ui, sans-serif",
      },
      grid: {
        vertLines: { color: 'rgba(229, 231, 235, 0.45)' },
        horzLines: { color: '#e5e7eb' },
      },
      rightPriceScale: {
        visible: true,
        borderVisible: false,
        scaleMargins: { top: 0.12, bottom: 0.12 },
        minimumWidth: 58,
      },
      leftPriceScale: {
        visible: false,
        borderVisible: false,
        scaleMargins: { top: 0.12, bottom: 0.12 },
        minimumWidth: 56,
        mode: PriceScaleMode.Normal,
      },
      timeScale: {
        borderVisible: false,
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        horzLine: { color: '#9aa5a1' },
        vertLine: { color: '#9aa5a1' },
      },
      ...fixedTimeAxisInteractionOptions(),
    })
    chartRef.current = chart
    const crosshairHandler = (param: MouseEventParams<Time>) => {
      if (modeRef.current === 'realtime' || !param.point || param.time === undefined) {
        onHoverPointRef.current?.(null)
        return
      }
      const point = pointAtChartTime(dataRef.current, param.time)
      onHoverPointRef.current?.(point)
    }
    chart.subscribeCrosshairMove(crosshairHandler)

    resizeObserverRef.current = new ResizeObserver(() => applyVisibleRange(chart, modeRef.current, dataRef.current))
    resizeObserverRef.current.observe(container)

    return () => {
      chart.unsubscribeCrosshairMove(crosshairHandler)
      resizeObserverRef.current?.disconnect()
      resizeObserverRef.current = null
      seriesRefs.current = []
      chart.remove()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    seriesRefs.current.forEach((series) => chart.removeSeries(series))
    seriesRefs.current = []

    if (!data.length) {
      chart.applyOptions({ leftPriceScale: { visible: false } })
      return
    }

    const percentData = buildPercentScalePoints(data, referencePrice)
    chart.applyOptions({ leftPriceScale: { visible: percentData.length > 0 } })

    if (mode === 'realtime') {
      addRealtimeTimeAxisAnchors(chart, data, seriesRefs.current)
      addLineSegments(
        chart,
        data,
        'close',
        '#38423f',
        2,
        seriesRefs.current,
        true,
        priceSeriesOptions(),
        markers,
        mode,
      )
      addLineSegments(
        chart,
        data.filter((point) => point.avgPrice !== null),
        'avgPrice',
        '#d9a229',
        2,
        seriesRefs.current,
        true,
        priceSeriesOptions(),
      )
      addLineSegments(chart, percentData, 'close', 'rgba(201, 76, 76, 0)', 1, seriesRefs.current, false, {
        priceScaleId: PERCENT_PRICE_SCALE_ID,
        priceFormat: percentPriceFormat(),
        crosshairMarkerVisible: false,
        lastValueVisible: true,
      })
      addLineSegments(
        chart,
        percentData.filter((point) => point.avgPrice !== null),
        'avgPrice',
        'rgba(217, 162, 41, 0)',
        1,
        seriesRefs.current,
        false,
        percentSeriesOptions(),
      )
    } else {
      const series = chart.addSeries(CandlestickSeries, {
        upColor: '#c94c4c',
        downColor: '#2f8a54',
        borderUpColor: '#c94c4c',
        borderDownColor: '#2f8a54',
        wickUpColor: '#c94c4c',
        wickDownColor: '#2f8a54',
        priceLineVisible: false,
        priceFormat: priceSeriesOptions().priceFormat,
      })
      seriesRefs.current.push(series)
      series.setData(
        data.map((point) => ({
          time: toChartTime(point),
          open: point.open,
          high: point.high,
          low: point.low,
          close: point.close,
        })) satisfies CandlestickData[],
      )
      addReplayMarkers(series, markers, mode)

      const percentSeries = chart.addSeries(CandlestickSeries, {
        upColor: 'rgba(201, 76, 76, 0)',
        downColor: 'rgba(47, 138, 84, 0)',
        borderUpColor: 'rgba(201, 76, 76, 0)',
        borderDownColor: 'rgba(47, 138, 84, 0)',
        wickUpColor: 'rgba(201, 76, 76, 0)',
        wickDownColor: 'rgba(47, 138, 84, 0)',
        lastValueVisible: true,
        priceLineVisible: false,
        priceScaleId: PERCENT_PRICE_SCALE_ID,
        priceFormat: percentPriceFormat(),
      })
      seriesRefs.current.push(percentSeries)
      percentSeries.setData(
        percentData.map((point) => ({
          time: toChartTime(point),
          open: point.open,
          high: point.high,
          low: point.low,
          close: point.close,
        })) satisfies CandlestickData[],
      )

      addMaSeries(chart, data, 'ma5', '#d19a22', seriesRefs.current)
      addMaSeries(chart, data, 'ma10', '#6b7bdc', seriesRefs.current)
      addMaSeries(chart, data, 'ma20', '#a15cbf', seriesRefs.current)
      addPercentMaSeries(chart, percentData, 'ma5', seriesRefs.current)
      addPercentMaSeries(chart, percentData, 'ma10', seriesRefs.current)
      addPercentMaSeries(chart, percentData, 'ma20', seriesRefs.current)
    }
    applyVisibleRange(chart, mode, data)
  }, [data, markers, mode, referencePrice])

  return (
    <div className="financial-chart-shell">
      <div ref={containerRef} className="financial-chart" />
      <PercentAxisOverlay axis={percentAxis} />
    </div>
  )
}

function addReplayMarkers(series: ChartSeries, markers: ChartMarker[], mode: ChartMode) {
  if (!markers.length) return
  createSeriesMarkers(
    series,
    markers.map((marker) => toSeriesMarker(marker, mode)),
    {
      autoScale: true,
      zOrder: 'top',
    },
  )
}

function toSeriesMarker(marker: ChartMarker, mode: ChartMode): SeriesMarker<Time> {
  const isBuy = marker.action === 'buy'
  const confirmed = marker.llmAction === marker.action
  const color = confirmed ? (isBuy ? '#c94c4c' : '#2f8a54') : '#687570'
  return {
    id: marker.id,
    time: toChartTime(marker),
    position: mode === 'realtime' ? (isBuy ? 'atPriceBottom' : 'atPriceTop') : isBuy ? 'belowBar' : 'aboveBar',
    price: marker.price,
    shape: isBuy ? 'arrowUp' : 'arrowDown',
    color,
    text: marker.label,
    size: confirmed ? 1.35 : 1.15,
  }
}

function PercentAxisOverlay({ axis }: { axis: PercentAxis | null }) {
  if (!axis) return null
  return (
    <div className="percent-axis-overlay" aria-hidden="true">
      {axis.ticks.map((tick) => (
        <span
          key={`${tick.label}-${tick.position.toFixed(2)}`}
          className={tick.value > 0 ? 'up' : tick.value < 0 ? 'down' : 'flat'}
          style={{ top: `${tick.position}%` }}
        >
          {tick.label}
        </span>
      ))}
    </div>
  )
}

function addMaSeries(
  chart: IChartApi,
  data: ChartPoint[],
  key: 'ma5' | 'ma10' | 'ma20',
  color: string,
  seriesRefs: ChartSeries[],
) {
  addLineSegments(
    chart,
    data.filter((point) => point[key] !== null),
    key,
    color,
    1,
    seriesRefs,
    false,
    priceSeriesOptions(),
  )
}

function addPercentMaSeries(
  chart: IChartApi,
  data: PercentScalePoint[],
  key: 'ma5' | 'ma10' | 'ma20',
  seriesRefs: ChartSeries[],
) {
  addLineSegments(
    chart,
    data.filter((point) => point[key] !== null),
    key,
    'rgba(0, 0, 0, 0)',
    1,
    seriesRefs,
    false,
    percentSeriesOptions(),
  )
}

function addRealtimeTimeAxisAnchors(chart: IChartApi, data: ChartPoint[], seriesRefs: ChartSeries[]) {
  const tradeDate = data[0]?.timestamp.slice(0, 10)
  if (!tradeDate) return

  const series = chart.addSeries(LineSeries, {
    color: 'rgba(0, 0, 0, 0)',
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
    autoscaleInfoProvider: () => null,
  })
  seriesRefs.push(series)
  series.setData(
    buildTradingDayTimeAnchors(tradeDate).map((anchor) => ({
      time: toChartTime(anchor),
    })) satisfies WhitespaceData[],
  )
}

function addLineSegments(
  chart: IChartApi,
  data: PriceLikePoint[],
  key: 'close' | 'avgPrice' | 'ma5' | 'ma10' | 'ma20',
  color: string,
  lineWidth: 1 | 2,
  seriesRefs: ChartSeries[],
  showLastValue = false,
  overrides: LineSeriesPartialOptions = {},
  markers: ChartMarker[] = [],
  mode?: ChartMode,
) {
  const segments = splitTradingSessions(data)
  segments.forEach((segment, index) => {
    const series = chart.addSeries(LineSeries, {
      color,
      lineWidth,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
      lastValueVisible: showLastValue && index === segments.length - 1,
      ...overrides,
    })
    seriesRefs.push(series)
    series.setData(
      segment.map((point) => ({
        time: toChartTime(point),
        value: Number(point[key]),
      })) satisfies LineData[],
    )
    if (mode && markers.length) {
      addReplayMarkers(series, filterMarkersForSegment(markers, segment), mode)
    }
  })
}

function splitTradingSessions<T extends PriceLikePoint>(data: T[]): T[][] {
  const segments: T[][] = []
  let current: T[] = []
  let currentSession = ''

  data.forEach((point) => {
    const session = tradingSession(point.time)
    if (!session) return

    if (current.length && session !== currentSession) {
      segments.push(current)
      current = []
    }

    current.push(point)
    currentSession = session
  })

  if (current.length) segments.push(current)
  return segments
}

function tradingSession(time: string) {
  if (time >= '09:30' && time <= '11:30') return 'morning'
  if (time >= '13:00' && time <= '15:00') return 'afternoon'
  return ''
}

function pointAtChartTime(data: ChartPoint[], time: Time) {
  if (typeof time !== 'number') return null
  return data.find((point) => toChartTime(point) === time) ?? null
}

function toChartTime(point: Pick<ChartPoint, 'timestamp'>): Time {
  const [datePart, timePart] = point.timestamp.split('T')
  const [year, month, day] = datePart.split('-').map(Number)
  const [hour, minute] = timePart.split(':').map(Number)
  return Math.floor(Date.UTC(year, month - 1, day, hour, minute, 0) / 1000) as UTCTimestamp
}

function percentPriceFormat() {
  return {
    type: 'custom' as const,
    minMove: 0.01,
    formatter: formatAxisPercent,
    tickmarksFormatter: (values: number[]) => values.map(formatAxisPercent),
  }
}

function percentSeriesOptions(): LineSeriesPartialOptions {
  return {
    priceScaleId: PERCENT_PRICE_SCALE_ID,
    priceFormat: percentPriceFormat(),
    crosshairMarkerVisible: false,
    lastValueVisible: false,
    priceLineVisible: false,
  }
}

function priceSeriesOptions(): LineSeriesPartialOptions {
  return {
    priceFormat: {
      type: 'price',
      precision: 2,
      minMove: 0.01,
    },
  }
}

function formatAxisPercent(value: number) {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}

function applyVisibleRange(chart: IChartApi, mode: ChartMode, data: ChartPoint[] = []) {
  if (mode === 'realtime') {
    const tradeDate = data[0]?.timestamp.slice(0, 10) ?? new Date().toISOString().slice(0, 10)
    chart.timeScale().setVisibleRange({
      from: toChartTime(emptyPointAt(tradeDate, '09:30')),
      to: toChartTime(emptyPointAt(tradeDate, '15:00')),
    })
    return
  }
  chart.timeScale().fitContent()
}

function emptyPointAt(tradeDate: string, time: string): ChartPoint {
  return {
    timestamp: `${tradeDate}T${time}:00`,
    time,
    open: 0,
    high: 0,
    low: 0,
    close: 0,
    volume: 0,
    ma5: null,
    ma10: null,
    ma20: null,
    avgPrice: null,
    change: 0,
    changePercent: 0,
    changeReference: 0,
  }
}
