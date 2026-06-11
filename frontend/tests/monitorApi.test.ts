import assert from 'node:assert/strict'
import test from 'node:test'

import { fetchMonitorStatus, startMonitor, stopMonitor } from '../src/monitorApi.ts'

const runtimeState = {
  running: true,
  last_tick_at: null,
  last_success_at: null,
  last_error: null,
  last_notified_signal_key: null,
  notification_count: 0,
}

test('startMonitor posts to the backend monitor start endpoint', async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = []
  const fetcher = async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init })
    return Response.json(runtimeState)
  }

  const state = await startMonitor('http://127.0.0.1:8000', fetcher)

  assert.equal(state.running, true)
  assert.equal(calls[0]?.url, 'http://127.0.0.1:8000/api/monitor/start')
  assert.equal(calls[0]?.init?.method, 'POST')
})

test('stopMonitor posts to the backend monitor stop endpoint', async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = []
  const fetcher = async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init })
    return Response.json({ ...runtimeState, running: false })
  }

  const state = await stopMonitor('http://127.0.0.1:8000', fetcher)

  assert.equal(state.running, false)
  assert.equal(calls[0]?.url, 'http://127.0.0.1:8000/api/monitor/stop')
  assert.equal(calls[0]?.init?.method, 'POST')
})

test('fetchMonitorStatus reads the backend monitor state without starting it', async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = []
  const fetcher = async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init })
    return Response.json({ ...runtimeState, running: false })
  }

  const state = await fetchMonitorStatus('http://127.0.0.1:8000', undefined, fetcher)

  assert.equal(state.running, false)
  assert.equal(calls[0]?.url, 'http://127.0.0.1:8000/api/monitor/status')
  assert.equal(calls[0]?.init?.method, 'GET')
})
