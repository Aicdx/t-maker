import assert from 'node:assert/strict'
import test from 'node:test'

import { apiErrorMessage } from '../src/apiErrors.ts'

test('apiErrorMessage uses structured backend detail message when available', async () => {
  const response = new Response(
    JSON.stringify({
      detail: {
        code: 'market_data_channel_unavailable',
        message: '300308 2026-05-29 本地没有分钟线缓存，行情源暂不可用。',
      },
    }),
    { status: 503 },
  )

  assert.equal(
    await apiErrorMessage(response),
    '300308 2026-05-29 本地没有分钟线缓存，行情源暂不可用。',
  )
})

test('apiErrorMessage falls back to status for non-json errors', async () => {
  const response = new Response('bad gateway', { status: 502 })

  assert.equal(await apiErrorMessage(response), 'HTTP 502')
})
