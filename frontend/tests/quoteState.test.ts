import assert from 'node:assert/strict'
import test from 'node:test'

import { quoteForVisibleContext } from '../src/quoteState.ts'

test('quoteForVisibleContext does not use live snapshot quote for a selected historical day without day quote', () => {
  const liveQuote = quote(748, 775.94)

  const selected = quoteForVisibleContext({
    selectedDayQuote: null,
    snapshotQuote: liveQuote,
    hasSelectedDay: true,
    hasRecentReplay: false,
    hasPlayback: false,
  })

  assert.equal(selected, undefined)
})

test('quoteForVisibleContext uses selected day quote when it exists', () => {
  const dayQuote = quote(679.82, 709.14)
  const liveQuote = quote(748, 775.94)

  const selected = quoteForVisibleContext({
    selectedDayQuote: dayQuote,
    snapshotQuote: liveQuote,
    hasSelectedDay: true,
    hasRecentReplay: false,
    hasPlayback: false,
  })

  assert.equal(selected, dayQuote)
})

test('quoteForVisibleContext uses live snapshot quote only for live snapshot view', () => {
  const liveQuote = quote(748, 775.94)

  const selected = quoteForVisibleContext({
    selectedDayQuote: null,
    snapshotQuote: liveQuote,
    hasSelectedDay: false,
    hasRecentReplay: false,
    hasPlayback: false,
    monitoring: false,
  })

  assert.equal(selected, liveQuote)
})

test('quoteForVisibleContext uses live snapshot quote while viewing realtime data with selected today', () => {
  const liveQuote = quote(1184.99, 1154.99)

  const selected = quoteForVisibleContext({
    selectedDayQuote: null,
    snapshotQuote: liveQuote,
    hasSelectedDay: true,
    hasRecentReplay: false,
    hasPlayback: false,
    usesLiveSnapshot: true,
  })

  assert.equal(selected, liveQuote)
})

function quote(latest: number, previousClose: number) {
  return {
    symbol: '300502',
    name: '新易盛',
    latest,
    previous_close: previousClose,
    open: latest,
    high: latest,
    low: latest,
    change: latest - previousClose,
    change_percent: ((latest - previousClose) / previousClose) * 100,
  }
}
