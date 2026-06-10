import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildTradeConfirmationRequest,
  formatTradeMoney,
  tradeConfirmationActionLabel,
} from '../src/tradeStats.ts'

test('buildTradeConfirmationRequest maps selected point to a 100-share buy confirmation', () => {
  const request = buildTradeConfirmationRequest(
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
    'buy',
    'monitor',
  )

  assert.deepEqual(request, {
    symbol: '300308',
    signal_timestamp: '2026-06-10T10:24:00',
    signal_action: 'buy',
    confirm_action: 'buy',
    price: 123.45,
    quantity: 100,
    source: 'monitor',
    reason: 'AI低吸点位',
    llm_confidence: 0.68,
  })
})

test('tradeConfirmationActionLabel names manual actions', () => {
  assert.equal(tradeConfirmationActionLabel('buy'), '低吸')
  assert.equal(tradeConfirmationActionLabel('sell'), '高抛')
})

test('formatTradeMoney formats gains losses and zero', () => {
  assert.equal(formatTradeMoney(156), '+156.00')
  assert.equal(formatTradeMoney(-12.5), '-12.50')
  assert.equal(formatTradeMoney(0), '0.00')
})
