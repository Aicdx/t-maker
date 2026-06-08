import assert from 'node:assert/strict'
import test from 'node:test'

import { isAshareTradingTime, monitorStatus } from '../src/marketHours.ts'

test('isAshareTradingTime includes A-share morning and afternoon sessions', () => {
  assert.equal(isAshareTradingTime(new Date(2026, 5, 8, 9, 30)), true)
  assert.equal(isAshareTradingTime(new Date(2026, 5, 8, 11, 30)), true)
  assert.equal(isAshareTradingTime(new Date(2026, 5, 8, 13, 0)), true)
  assert.equal(isAshareTradingTime(new Date(2026, 5, 8, 15, 0)), true)
})

test('isAshareTradingTime excludes lunch break after close and weekends', () => {
  assert.equal(isAshareTradingTime(new Date(2026, 5, 8, 11, 31)), false)
  assert.equal(isAshareTradingTime(new Date(2026, 5, 8, 15, 1)), false)
  assert.equal(isAshareTradingTime(new Date(2026, 5, 7, 10, 0)), false)
})

test('monitorStatus pauses enabled monitoring outside trading time', () => {
  assert.equal(monitorStatus(false, new Date(2026, 5, 8, 10, 0)), 'off')
  assert.equal(monitorStatus(true, new Date(2026, 5, 8, 10, 0)), 'active')
  assert.equal(monitorStatus(true, new Date(2026, 5, 8, 12, 0)), 'paused')
})
