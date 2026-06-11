import { apiErrorMessage } from './apiErrors.ts'

export type MonitorRuntimeState = {
  running: boolean
  last_tick_at: string | null
  last_success_at: string | null
  last_error: string | null
  last_notified_signal_key: string | null
  notification_count: number
}

type Fetcher = typeof fetch

export function fetchMonitorStatus(apiBase: string, signal?: AbortSignal, fetcher: Fetcher = fetch) {
  return monitorRequest(apiBase, '/api/monitor/status', { signal, fetcher })
}

export function startMonitor(apiBase: string, fetcher: Fetcher = fetch) {
  return monitorRequest(apiBase, '/api/monitor/start', { method: 'POST', fetcher })
}

export function stopMonitor(apiBase: string, fetcher: Fetcher = fetch) {
  return monitorRequest(apiBase, '/api/monitor/stop', { method: 'POST', fetcher })
}

async function monitorRequest(
  apiBase: string,
  path: string,
  {
    method = 'GET',
    signal,
    fetcher,
  }: {
    method?: 'GET' | 'POST'
    signal?: AbortSignal
    fetcher: Fetcher
  },
) {
  const response = await fetcher(`${apiBase}${path}`, { method, signal })
  if (!response.ok) throw new Error(await apiErrorMessage(response))
  return (await response.json()) as MonitorRuntimeState
}
