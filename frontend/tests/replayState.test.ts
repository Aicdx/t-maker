import assert from 'node:assert/strict'
import test from 'node:test'

import {
  dayMarketPayloadToReplay,
  replayPointReviewLabel,
  replaySourceLabel,
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

test('replayPointReviewLabel does not display pending points as AI hold', () => {
  assert.equal(replayPointReviewLabel({ llm_status: 'pending', llm_action: null }), 'AI待复核')
  assert.equal(replayPointReviewLabel({ llm_status: 'ok', llm_action: 'hold' }), 'AI观望')
  assert.equal(replayPointReviewLabel({ llm_status: 'ok', llm_action: 'sell' }), 'AI高抛')
})
