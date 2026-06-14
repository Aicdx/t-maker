from datetime import datetime

from tmaker.strategy.t_pairing import (
    LiveTTradePlanner,
    TradeActionPoint,
    select_open_reprice_pairs,
)


def _point(
    id: str,
    action: str,
    price: float,
    minute: int,
    *,
    symbol: str = "300308",
) -> TradeActionPoint:
    return TradeActionPoint(
        id=id,
        symbol=symbol,
        timestamp=datetime(2026, 6, 10, 10, minute),
        action=action,
        price=price,
    )


def test_live_planner_reprices_open_and_waits_for_directional_close_floor() -> None:
    planner = LiveTTradePlanner()

    assert planner.should_notify(_point("sell-open", "sell", 100.0, 0)) is True
    assert planner.should_notify(_point("sell-better", "sell", 102.0, 1)) is True
    assert planner.should_notify(_point("buy-too-close", "buy", 101.4, 2)) is False
    assert planner.should_notify(_point("buy-close", "buy", 100.8, 3)) is True


def test_select_open_reprice_pairs_uses_best_same_direction_open_before_close() -> None:
    result = select_open_reprice_pairs(
        [
            _point("buy-early", "buy", 100.0, 0),
            _point("buy-better", "buy", 98.0, 1),
            _point("sell-close", "sell", 101.0, 2),
        ]
    )

    assert len(result.pairs) == 1
    assert result.pairs[0].buy.id == "buy-better"
    assert result.pairs[0].sell.id == "sell-close"
    assert result.pairs[0].spread == 3.0
    assert [point.id for point in result.unpaired] == ["buy-early"]


def test_select_open_reprice_pairs_can_wait_for_profitable_close_floor() -> None:
    result = select_open_reprice_pairs(
        [
            _point("sell-open", "sell", 102.0, 0),
            _point("buy-too-close", "buy", 101.4, 1),
            _point("buy-close", "buy", 100.8, 2),
        ],
        require_directional_close=True,
    )

    assert len(result.pairs) == 1
    assert result.pairs[0].sell.id == "sell-open"
    assert result.pairs[0].buy.id == "buy-close"
    assert result.pairs[0].spread_pct >= 1.0
    assert [point.id for point in result.unpaired] == ["buy-too-close"]
