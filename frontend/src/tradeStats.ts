import type { ReplayPoint } from './replayState'

export type TradeConfirmationAction = 'buy' | 'sell'

export type TradeConfirmationRequest = {
  symbol: string
  signal_timestamp: string
  signal_action: ReplayPoint['action']
  confirm_action: TradeConfirmationAction
  price: number
  quantity: number
  source: string
  reason: string
  llm_confidence: number | null
}

export type TradeConfirmation = TradeConfirmationRequest & {
  id: string
  trade_date: string
  created_at: string
}

export type TradeConfirmationPair = {
  symbol: string
  buy_id: string
  sell_id: string
  buy_price: number
  sell_price: number
  quantity: number
  spread: number
  pnl: number
  opened_at: string
  closed_at: string
}

export type TradeConfirmationStats = {
  date: string
  quantity_per_trade: number
  summary: {
    record_count: number
    paired_count: number
    unpaired_count: number
    total_pnl: number
  }
  pairs: TradeConfirmationPair[]
  unpaired: TradeConfirmation[]
}

export type TradeConfirmationSummary = TradeConfirmationStats['summary']

export type TradeConfirmationSummaryReport = {
  start_date: string
  end_date: string
  symbol: string | null
  summary: TradeConfirmationSummary
  by_date: {
    date: string
    summary: TradeConfirmationSummary
  }[]
  by_symbol: {
    symbol: string
    summary: TradeConfirmationSummary
  }[]
}

export function buildTradeConfirmationRequest(
  point: ReplayPoint,
  confirmAction: TradeConfirmationAction,
  source: string,
): TradeConfirmationRequest {
  return {
    symbol: point.symbol,
    signal_timestamp: point.timestamp,
    signal_action: point.action,
    confirm_action: confirmAction,
    price: point.price,
    quantity: 100,
    source,
    reason: point.reason,
    llm_confidence: point.llm_confidence ?? null,
  }
}

export function tradeConfirmationActionLabel(action: TradeConfirmationAction) {
  if (action === 'buy') return '低吸'
  return '高抛'
}

export function formatTradeMoney(value: number) {
  if (value > 0) return `+${value.toFixed(2)}`
  return value.toFixed(2)
}
