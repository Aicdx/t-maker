export type MarketQuoteLike = {
  symbol: string
  name: string
  latest: number
  previous_close: number
  open: number
  high: number
  low: number
  change: number
  change_percent: number
}

export function quoteForVisibleContext<TQuote extends MarketQuoteLike>({
  selectedDayQuote,
  snapshotQuote,
  hasSelectedDay,
  hasRecentReplay,
  hasPlayback,
}: {
  selectedDayQuote?: TQuote | null
  snapshotQuote?: TQuote
  hasSelectedDay: boolean
  hasRecentReplay: boolean
  hasPlayback: boolean
}) {
  if (hasSelectedDay) return selectedDayQuote ?? undefined
  if (hasRecentReplay || hasPlayback) return undefined
  return snapshotQuote
}
