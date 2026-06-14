from datetime import date, datetime

from tmaker.domain.models import (
    TradeConfirmation,
    TradeConfirmationAction,
    build_trade_confirmation_stats,
)


def _confirmation(
    *,
    id: str,
    symbol: str = "300308",
    signal_timestamp: datetime,
    confirm_action: TradeConfirmationAction,
    price: float,
) -> TradeConfirmation:
    return TradeConfirmation(
        id=id,
        symbol=symbol,
        trade_date=signal_timestamp.date(),
        signal_timestamp=signal_timestamp,
        signal_action=confirm_action,
        confirm_action=confirm_action,
        price=price,
        quantity=100,
        source="monitor",
        reason="AI 点位确认",
        llm_confidence=0.72,
        created_at=signal_timestamp,
    )


def test_trade_confirmation_stats_pairs_buy_then_sell() -> None:
    stats = build_trade_confirmation_stats(
        [
            _confirmation(
                id="buy-1",
                signal_timestamp=datetime(2026, 6, 10, 10, 24),
                confirm_action=TradeConfirmationAction.BUY,
                price=123.4,
            ),
            _confirmation(
                id="sell-1",
                signal_timestamp=datetime(2026, 6, 10, 13, 12),
                confirm_action=TradeConfirmationAction.SELL,
                price=125.1,
            ),
        ],
        trade_date=date(2026, 6, 10),
    )

    assert stats.summary.record_count == 2
    assert stats.summary.paired_count == 1
    assert stats.summary.unpaired_count == 0
    assert stats.summary.total_pnl == 170.0
    assert stats.pairs[0].buy_id == "buy-1"
    assert stats.pairs[0].sell_id == "sell-1"
    assert stats.pairs[0].spread == 1.7
    assert stats.pairs[0].pnl == 170.0


def test_trade_confirmation_stats_pairs_sell_then_lower_buy() -> None:
    stats = build_trade_confirmation_stats(
        [
            _confirmation(
                id="sell-1",
                signal_timestamp=datetime(2026, 6, 10, 10, 5),
                confirm_action=TradeConfirmationAction.SELL,
                price=126.0,
            ),
            _confirmation(
                id="buy-1",
                signal_timestamp=datetime(2026, 6, 10, 14, 6),
                confirm_action=TradeConfirmationAction.BUY,
                price=124.8,
            ),
        ],
        trade_date=date(2026, 6, 10),
    )

    assert stats.summary.total_pnl == 120.0
    assert stats.pairs[0].opened_at == datetime(2026, 6, 10, 10, 5)
    assert stats.pairs[0].closed_at == datetime(2026, 6, 10, 14, 6)


def test_trade_confirmation_stats_reprices_same_direction_open_before_pairing() -> None:
    stats = build_trade_confirmation_stats(
        [
            _confirmation(
                id="buy-early",
                signal_timestamp=datetime(2026, 6, 10, 10, 1),
                confirm_action=TradeConfirmationAction.BUY,
                price=100.0,
            ),
            _confirmation(
                id="buy-better",
                signal_timestamp=datetime(2026, 6, 10, 10, 2),
                confirm_action=TradeConfirmationAction.BUY,
                price=98.0,
            ),
            _confirmation(
                id="sell-close",
                signal_timestamp=datetime(2026, 6, 10, 10, 3),
                confirm_action=TradeConfirmationAction.SELL,
                price=101.0,
            ),
        ],
        trade_date=date(2026, 6, 10),
    )

    assert stats.summary.paired_count == 1
    assert stats.summary.unpaired_count == 1
    assert stats.summary.total_pnl == 300.0
    assert stats.pairs[0].buy_id == "buy-better"
    assert stats.pairs[0].sell_id == "sell-close"
    assert [item.id for item in stats.unpaired] == ["buy-early"]


def test_trade_confirmation_stats_isolates_symbols_and_keeps_unpaired_records() -> None:
    stats = build_trade_confirmation_stats(
        [
            _confirmation(
                id="buy-300308",
                symbol="300308",
                signal_timestamp=datetime(2026, 6, 10, 10, 24),
                confirm_action=TradeConfirmationAction.BUY,
                price=123.4,
            ),
            _confirmation(
                id="sell-600487",
                symbol="600487",
                signal_timestamp=datetime(2026, 6, 10, 10, 25),
                confirm_action=TradeConfirmationAction.SELL,
                price=28.3,
            ),
        ],
        trade_date=date(2026, 6, 10),
    )

    assert stats.summary.record_count == 2
    assert stats.summary.paired_count == 0
    assert stats.summary.unpaired_count == 2
    assert [item.id for item in stats.unpaired] == ["buy-300308", "sell-600487"]
