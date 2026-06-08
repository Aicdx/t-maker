export type MonitorStatus = 'off' | 'active' | 'paused'

export function isAshareTradingTime(now = new Date()) {
  const day = now.getDay()
  if (day === 0 || day === 6) return false
  const minutes = now.getHours() * 60 + now.getMinutes()
  return (
    (minutes >= minuteOfDay('09:30') && minutes <= minuteOfDay('11:30')) ||
    (minutes >= minuteOfDay('13:00') && minutes <= minuteOfDay('15:00'))
  )
}

export function monitorStatus(enabled: boolean, now = new Date()): MonitorStatus {
  if (!enabled) return 'off'
  return isAshareTradingTime(now) ? 'active' : 'paused'
}

function minuteOfDay(value: string) {
  const [hour, minute] = value.split(':').map(Number)
  return hour * 60 + minute
}
