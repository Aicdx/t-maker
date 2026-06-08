from datetime import datetime
from datetime import timedelta

from tmaker.domain.models import Candle, Position, Signal, SignalAction, SignalKind
from tmaker.strategy.replay import _compact_points, _to_replay_candidate, replay_symbol_today, replay_today


def test_compact_points_defaults_to_first_trigger_for_strict_replay() -> None:
    candidates = [
        _candidate("2026-06-05T10:41:00", 10.10),
        _candidate("2026-06-05T10:42:00", 9.92),
        _candidate("2026-06-05T10:43:00", 9.80),
    ]

    [point] = _compact_points(candidates)

    assert point.point.timestamp == "2026-06-05T10:41:00"
    assert point.point.price == 10.10


def test_compact_points_can_still_run_optimized_analysis_when_requested() -> None:
    candidates = [
        _candidate("2026-06-05T10:41:00", 10.10),
        _candidate("2026-06-05T10:42:00", 9.92),
        _candidate("2026-06-05T10:43:00", 9.80),
    ]

    [point] = _compact_points(candidates, strict=False)

    assert point.point.timestamp == "2026-06-05T10:43:00"
    assert point.point.price == 9.80


def test_compact_points_uses_first_visible_trigger_for_intraday_gain_sell_cluster() -> None:
    candidates = [
        _candidate(
            "2026-06-05T11:25:00",
            1307.40,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
        _candidate(
            "2026-06-05T11:30:00",
            1315.51,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
    ]

    [point] = _compact_points(candidates)

    assert point.point.timestamp == "2026-06-05T11:25:00"
    assert point.point.price == 1307.40


def test_compact_points_does_not_merge_across_lunch_break() -> None:
    candidates = [
        _candidate(
            "2026-06-05T11:30:00",
            1315.51,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
        _candidate(
            "2026-06-05T13:00:00",
            1318.51,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
    ]

    points = _compact_points(candidates)

    assert [point.point.timestamp for point in points] == [
        "2026-06-05T11:30:00",
        "2026-06-05T13:00:00",
    ]


def test_compact_points_merges_identical_sell_price_across_lunch_break() -> None:
    candidates = [
        _candidate(
            "2026-06-05T11:30:00",
            1315.51,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
        _candidate(
            "2026-06-05T13:00:00",
            1315.51,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
    ]

    points = _compact_points(candidates)

    assert [point.point.timestamp for point in points] == ["2026-06-05T11:30:00"]


def test_compact_points_keeps_prelunch_peak_when_sell_cluster_continues_after_lunch() -> None:
    candidates = [
        _candidate(
            "2026-06-05T11:30:00",
            1315.51,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
        _candidate(
            "2026-06-05T13:00:00",
            1315.51,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
        _candidate(
            "2026-06-05T13:01:00",
            1308.87,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
    ]

    [point] = _compact_points(candidates)

    assert point.point.timestamp == "2026-06-05T11:30:00"


def test_compact_points_keeps_new_high_after_lunch_as_separate_sell_point() -> None:
    candidates = [
        _candidate(
            "2026-06-05T11:30:00",
            1315.51,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
        _candidate(
            "2026-06-05T13:00:00",
            1315.51,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
        _candidate(
            "2026-06-05T13:14:00",
            1318.77,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
    ]

    points = _compact_points(candidates)

    assert [point.point.timestamp for point in points] == [
        "2026-06-05T11:30:00",
        "2026-06-05T13:14:00",
    ]


def test_compact_points_merges_sell_cluster_with_market_context_downgrade_rule() -> None:
    candidates = [
        _candidate(
            "2026-06-05T10:03:00",
            1278.49,
            action=SignalAction.SELL,
            kind=SignalKind.SUSPECTED,
            rule_ids=["intraday_gain_session_vwap_stretch", "market_sector_uptrend_sell_downgrade"],
        ),
        _candidate(
            "2026-06-05T10:04:00",
            1276.51,
            action=SignalAction.SELL,
            kind=SignalKind.CANDIDATE_SELL,
            rule_ids=["intraday_gain_session_vwap_stretch"],
        ),
    ]

    [point] = _compact_points(candidates)

    assert point.point.timestamp == "2026-06-05T10:03:00"


def test_symbol_replay_keeps_later_candidates_until_sell_is_confirmed() -> None:
    candles = _intraday_sell_then_buy_candles()
    provider = _Provider(candles)
    positions = [
        Position(symbol="300308", base_quantity=200, cost_price=0, available_cash=200000, t_quantity=100)
    ]

    result = replay_symbol_today(provider, "300308", positions)

    assert result.points[0].action == "sell"
    assert any(point.action == "buy" for point in result.points)
    assert all("restore_after_intraday_sell" not in point.rule_ids for point in result.points)


def test_symbol_replay_keeps_later_candidates_until_buy_is_confirmed() -> None:
    candles = _intraday_buy_then_sell_candles()
    provider = _Provider(candles)
    positions = [
        Position(symbol="300308", base_quantity=200, cost_price=0, available_cash=200000, t_quantity=100)
    ]

    result = replay_symbol_today(provider, "300308", positions)

    assert result.points[0].action == "buy"
    assert any(point.action == "sell" for point in result.points)
    assert all("restore_after_intraday_buy" not in point.rule_ids for point in result.points)


def test_reviewed_replay_does_not_restore_after_candidate_sell_when_model_holds() -> None:
    candles = _intraday_sell_then_buy_candles()
    provider = _Provider(candles)
    positions = [
        Position(symbol="300308", base_quantity=200, cost_price=0, available_cash=200000, t_quantity=100)
    ]
    review_client = _AlwaysHoldReviewClient()

    result = replay_today(provider, ["300308"], positions, review_client=review_client)

    buy_points = [point for point in result.points if point.action == "buy"]
    assert buy_points
    assert all("restore_after_intraday_sell" not in point.rule_ids for point in buy_points)


def test_reviewed_replay_restores_after_model_confirms_sell() -> None:
    candles = _intraday_sell_then_buy_candles()
    provider = _Provider(candles)
    positions = [
        Position(symbol="300308", base_quantity=200, cost_price=0, available_cash=200000, t_quantity=100)
    ]
    review_client = _SellThenHoldReviewClient()

    result = replay_today(provider, ["300308"], positions, review_client=review_client)

    buy_points = [point for point in result.points if point.action == "buy"]
    assert buy_points
    assert "restore_after_intraday_sell" in buy_points[0].rule_ids


def test_reviewed_replay_passes_market_context_to_model() -> None:
    stock = _candles(
        [100, 102, 104, 106, 108, 112],
        [100, 110, 120, 130, 140, 150],
        symbol="300308",
    )
    peer = _candles(
        [100, 100.5, 101, 101.2, 101.4, 101.5],
        [100, 100, 100, 100, 100, 100],
        symbol="300502",
    )
    provider = _Provider([*stock, *peer])
    positions = [
        Position(symbol="300308", base_quantity=200, cost_price=0, available_cash=200000, t_quantity=100),
        Position(symbol="300502", base_quantity=200, cost_price=0, available_cash=200000, t_quantity=100),
    ]
    review_client = _CaptureContextReviewClient()

    replay_today(provider, ["300308", "300502"], positions, review_client=review_client)

    contexts = [context for context in review_client.contexts if context["symbol"] == "300308"]
    assert contexts
    assert contexts[0]["market_context"] is not None
    assert "stock_vs_sector_pct" in contexts[0]["market_context"]


def _candidate(
    timestamp: str,
    price: float,
    action: SignalAction = SignalAction.BUY,
    kind: SignalKind = SignalKind.CANDIDATE_BUY,
    rule_ids: list[str] | None = None,
):
    signal = Signal(
        symbol="300308",
        timestamp=datetime.fromisoformat(timestamp),
        kind=kind,
        action=action,
        confidence=0.72,
        rule_ids=rule_ids or ["sharp_drop_shrinking_volume"],
        reason="急跌后量能收缩，并且价格明显低于 VWAP" if action == SignalAction.BUY else "强势冲高高抛",
        risks=[],
        source_fresh=True,
        llm_status="pending",
    )
    return _to_replay_candidate(
        signal,
        price,
        [
            Candle(
                symbol="300308",
                timestamp=signal.timestamp,
                open=price,
                high=price + 0.1,
                low=price - 0.1,
                close=price,
                volume=1000,
            )
        ],
        Position(symbol="300308", base_quantity=0, cost_price=0, available_cash=20000, t_quantity=100),
        [signal],
    )


class _Provider:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles

    def fetch_minutes(self, symbol: str) -> list[Candle]:
        return [candle for candle in self.candles if candle.symbol == symbol]


class _AlwaysHoldReviewClient:
    async def create_review(self, context: dict) -> dict:
        return {
            "action": "hold",
            "confidence": 0.55,
            "summary": "观察但不执行",
            "reasons": ["市场确认不足"],
            "risks": ["可能继续波动"],
            "wait_for": ["等待确认"],
            "execution_allowed": False,
            "execution_blockers": ["市场信号未确认"],
        }


class _SellThenHoldReviewClient:
    def __init__(self) -> None:
        self.calls = 0

    async def create_review(self, context: dict) -> dict:
        self.calls += 1
        action = "sell" if self.calls == 1 else "hold"
        return {
            "action": action,
            "confidence": 0.66,
            "summary": "确认高抛" if action == "sell" else "观察但不执行",
            "reasons": ["市场信号成立" if action == "sell" else "市场确认不足"],
            "risks": ["可能继续波动"],
            "wait_for": ["等待确认"],
            "execution_allowed": action == "sell",
            "execution_blockers": [] if action == "sell" else ["市场信号未确认"],
        }


def _intraday_sell_then_buy_candles() -> list[Candle]:
    return _candles([10.0, 10.1, 10.2, 10.8, 11.0, 10.7, 10.2, 9.7, 9.2], [100, 110, 120, 180, 220, 300, 260, 220, 180])


def _intraday_buy_then_sell_candles() -> list[Candle]:
    return _candles([10.5, 10.2, 9.9, 9.6, 9.55, 9.8, 10.2, 10.8, 11.2], [500, 420, 340, 260, 180, 220, 260, 320, 420])


class _CaptureContextReviewClient:
    def __init__(self) -> None:
        self.contexts: list[dict] = []

    async def create_review(self, context: dict) -> dict:
        self.contexts.append(context)
        return {
            "action": "sell",
            "confidence": 0.66,
            "summary": "确认高抛",
            "reasons": ["市场信号成立"],
            "risks": ["可能继续波动"],
            "wait_for": ["等待确认"],
            "execution_allowed": True,
            "execution_blockers": [],
        }


def _candles(closes: list[float], volumes: list[float], symbol: str = "300308") -> list[Candle]:
    start = datetime(2026, 6, 5, 10, 1)
    return [
        Candle(
            symbol=symbol,
            timestamp=start + timedelta(minutes=index),
            open=close,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]
