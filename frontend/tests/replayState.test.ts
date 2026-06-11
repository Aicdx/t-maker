import assert from 'node:assert/strict'
import test from 'node:test'

import {
  chartTradeDateLabel,
  dayMarketPayloadToReplay,
  replayPointReviewLabel,
  replayPointKey,
  replaySourceLabel,
  selectedReplayPointForKey,
  shiftCalendarDate,
} from '../src/replayState.ts'

test('dayMarketPayloadToReplay keeps stored replay points from the selected trading day', () => {
  const replay = dayMarketPayloadToReplay({
    symbol: '300308',
    date: '2026-06-05',
    chart_series: { realtime: [], one_minute: [], five_minute: [] },
    points: [
      {
        symbol: '300308',
        timestamp: '2026-06-05T10:05:00',
        action: 'buy',
        kind: 'candidate_buy',
        price: 150.2,
        confidence: 0.72,
        rule_ids: ['vwap_deviation'],
        reason: '历史低吸候选',
        risks: ['趋势仍弱'],
        llm_status: 'ok',
        llm_action: 'buy',
        llm_confidence: 0.66,
        llm_summary: '历史复核点位',
        llm_reasons: [],
        wait_for: [],
        execution_blockers: [],
      },
    ],
  })

  assert.equal(replay.points.length, 1)
  assert.equal(replay.points[0]?.timestamp, '2026-06-05T10:05:00')
  assert.equal(replay.summary.candidate_count, 1)
  assert.equal(replay.summary.buy_count, 1)
  assert.equal(replay.summary.reviewed_count, 1)
})

test('replaySourceLabel names selected-date points as stored history instead of replay', () => {
  assert.equal(
    replaySourceLabel({
      hasRecentReplay: false,
      playbackActive: false,
    }),
    '历史点位',
  )
})

test('replaySourceLabel keeps replay labels for active review and recent replay modes', () => {
  assert.equal(
    replaySourceLabel({
      hasRecentReplay: false,
      playbackActive: true,
    }),
    'AI复核',
  )
  assert.equal(
    replaySourceLabel({
      hasRecentReplay: true,
      recentReviewEnabled: false,
      playbackActive: false,
    }),
    '快速回放',
  )
  assert.equal(
    replaySourceLabel({
      hasRecentReplay: true,
      recentReviewEnabled: true,
      playbackActive: false,
    }),
    'AI复核',
  )
})

test('replaySourceLabel names live monitoring points separately from stored history', () => {
  assert.equal(
    replaySourceLabel({
      hasRecentReplay: false,
      monitoring: true,
      playbackActive: false,
    }),
    '实时盯盘',
  )
})

test('shiftCalendarDate moves by natural days instead of cached trading-day options', () => {
  assert.equal(shiftCalendarDate('2026-06-01', -1), '2026-05-31')
  assert.equal(shiftCalendarDate('2026-06-01', 1), '2026-06-02')
})

test('chartTradeDateLabel shows realtime snapshot date while monitoring', () => {
  assert.equal(
    chartTradeDateLabel({
      monitorEnabled: true,
      selectedTradeDate: '2026-06-08',
      latestRealtimeTimestamp: '2026-06-09T10:23:00',
    }),
    '2026-06-09',
  )
  assert.equal(
    chartTradeDateLabel({
      monitorEnabled: false,
      selectedTradeDate: '2026-06-08',
      latestRealtimeTimestamp: '2026-06-09T10:23:00',
    }),
    '2026-06-08',
  )
})

test('replayPointReviewLabel does not display pending points as AI hold', () => {
  assert.equal(replayPointReviewLabel({ llm_status: 'pending', llm_action: null }), 'AI复核中')
  assert.equal(replayPointReviewLabel({ llm_status: 'ok', llm_action: 'hold' }), 'AI观望')
  assert.equal(replayPointReviewLabel({ llm_status: 'ok', llm_action: 'sell' }), 'AI高抛')
})

test('selectedReplayPointForKey only returns a point after an explicit selection', () => {
  const points = [
    {
      symbol: '300308',
      timestamp: '2026-06-10T10:24:00',
      action: 'buy',
      kind: 'candidate_buy',
      price: 123.45,
      confidence: 0.72,
      rule_ids: ['vwap_deviation'],
      reason: 'AI低吸点位',
      risks: [],
      llm_status: 'ok',
      llm_action: 'buy',
      llm_confidence: 0.68,
      llm_summary: '复核通过',
      llm_reasons: [],
      wait_for: [],
      execution_blockers: [],
    },
    {
      symbol: '300308',
      timestamp: '2026-06-10T13:18:00',
      action: 'sell',
      kind: 'candidate_sell',
      price: 128.1,
      confidence: 0.7,
      rule_ids: ['profit_band'],
      reason: 'AI高抛点位',
      risks: [],
      llm_status: 'ok',
      llm_action: 'sell',
      llm_confidence: 0.64,
      llm_summary: '复核通过',
      llm_reasons: [],
      wait_for: [],
      execution_blockers: [],
    },
  ] as const

  assert.equal(selectedReplayPointForKey(points, null), null)
  assert.equal(selectedReplayPointForKey(points, replayPointKey(points[0])), points[0])
  assert.equal(selectedReplayPointForKey(points, 'missing'), null)
})
