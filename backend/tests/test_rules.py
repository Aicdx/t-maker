from datetime import datetime, timedelta

from tmaker.domain.models import Candle, Position, ProviderHealth, SignalAction, SignalKind
from tmaker.strategy.indicators import compute_indicators
from tmaker.strategy.rules import MarketContext, evaluate_signal


def series(closes: list[float], volumes: list[float]) -> list[Candle]:
    start = datetime(2026, 6, 5, 10, 1)
    return [
        Candle(
            symbol="600000",
            timestamp=start + timedelta(minutes=index),
            open=close + 0.05,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]


def position() -> Position:
    return Position(symbol="600000", base_quantity=1000, cost_price=10, available_cash=10000, t_quantity=300)


def fresh_health() -> ProviderHealth:
    return ProviderHealth(
        provider="akshare",
        symbol="600000",
        last_success_at=datetime(2026, 6, 5, 10, 5),
        stale_after_seconds=90,
    )


def fresh_health_at(timestamp: datetime) -> ProviderHealth:
    return ProviderHealth(
        provider="akshare",
        symbol="600000",
        last_success_at=timestamp,
        stale_after_seconds=90,
    )


def test_evaluate_signal_emits_buy_for_sharp_drop_with_shrinking_volume() -> None:
    candles = series([10.5, 10.2, 9.9, 9.6, 9.55], [500, 420, 340, 260, 180])

    signal = evaluate_signal(candles, [], position(), fresh_health(), now=datetime(2026, 6, 5, 10, 5))

    assert signal.kind == SignalKind.CANDIDATE_BUY
    assert signal.action == SignalAction.BUY
    assert "sharp_drop_shrinking_volume" in signal.rule_ids
    assert signal.needs_llm_review is True


def test_evaluate_signal_emits_sell_for_high_vwap_deviation() -> None:
    candles = series([10.0, 10.1, 10.2, 10.8, 11.0], [100, 110, 120, 180, 220])

    signal = evaluate_signal(candles, [], position(), fresh_health(), now=datetime(2026, 6, 5, 10, 5))

    assert signal.kind == SignalKind.CANDIDATE_SELL
    assert signal.action == SignalAction.SELL
    assert "vwap_high_sell" in signal.rule_ids


def test_evaluate_signal_emits_sell_for_intraday_gain_far_above_session_vwap() -> None:
    session = series(
        [1220, 1249, 1256, 1268, 1279, 1285, 1296, 1307, 1315],
        [4500, 16000, 8000, 4000, 3000, 2500, 3200, 7800, 5300],
    )
    rolling_window = session[-5:]

    signal = evaluate_signal(
        rolling_window,
        [],
        position(),
        fresh_health_at(rolling_window[-1].timestamp),
        now=rolling_window[-1].timestamp,
        session_candles=session,
    )

    assert signal.kind == SignalKind.CANDIDATE_SELL
    assert signal.action == SignalAction.SELL
    assert "intraday_gain_session_vwap_stretch" in signal.rule_ids
    assert "全天均价" in signal.reason


def test_evaluate_signal_downgrades_sell_when_market_and_sector_trend_together() -> None:
    session = series(
        [100, 102, 104, 106, 108, 110],
        [1000, 1100, 1200, 1300, 1400, 1500],
    )
    market_context = MarketContext(
        index_change_pct=1.2,
        sector_change_pct=4.6,
        sector_relative_strength_pct=3.4,
        stock_vs_sector_pct=0.1,
        sector_trend="up",
        index_trend="up",
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
        market_context=market_context,
    )

    assert signal.kind == SignalKind.SUSPECTED
    assert signal.action == SignalAction.SELL
    assert "market_sector_uptrend_sell_downgrade" in signal.rule_ids
    assert "共振走强" in signal.reason


def test_evaluate_signal_downgrades_sell_when_sector_is_strong_without_index_data() -> None:
    session = series(
        [100, 102, 104, 106, 108, 110],
        [1000, 1100, 1200, 1300, 1400, 1500],
    )
    market_context = MarketContext(
        index_change_pct=0,
        sector_change_pct=5.0,
        sector_relative_strength_pct=5.0,
        stock_vs_sector_pct=0.2,
        sector_trend="up",
        index_trend="unknown",
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
        market_context=market_context,
    )

    assert signal.kind == SignalKind.SUSPECTED
    assert "market_sector_uptrend_sell_downgrade" in signal.rule_ids
    assert "板块代理强势上行" in signal.reason
    assert "缺少可用大盘指数数据" in signal.reason


def test_evaluate_signal_enhances_sell_when_stock_outperforms_weak_sector_near_high() -> None:
    session = series(
        [100, 101, 103, 106, 109, 112],
        [1000, 1100, 1200, 1300, 1400, 1800],
    )
    market_context = MarketContext(
        index_change_pct=-0.3,
        sector_change_pct=1.0,
        sector_relative_strength_pct=1.3,
        stock_vs_sector_pct=11.0,
        sector_trend="down",
        index_trend="down",
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
        market_context=market_context,
    )

    assert signal.kind == SignalKind.CANDIDATE_SELL
    assert signal.action == SignalAction.SELL
    assert "market_context_sell_boost" in signal.rule_ids
    assert "板块或大盘转弱" in signal.reason


def test_evaluate_signal_does_not_emit_candidate_sell_after_pullback_from_session_high() -> None:
    session = series(
        [1220, 1260, 1290, 1318, 1302, 1296],
        [4500, 9000, 8000, 12000, 7000, 6000],
    )
    market_context = MarketContext(
        index_change_pct=0,
        sector_change_pct=4.8,
        sector_relative_strength_pct=4.8,
        stock_vs_sector_pct=1.1,
        sector_trend="down",
        index_trend="unknown",
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
        market_context=market_context,
    )

    assert signal.kind != SignalKind.CANDIDATE_SELL
    assert "intraday_gain_session_vwap_stretch" not in signal.rule_ids


def test_evaluate_signal_emits_suspected_buy_for_moderate_drop_below_vwap() -> None:
    candles = series([10.5, 10.35, 10.18, 10.02, 9.95], [500, 470, 440, 410, 390])

    signal = evaluate_signal(candles, [], position(), fresh_health(), now=datetime(2026, 6, 5, 10, 5))

    assert signal.kind == SignalKind.SUSPECTED
    assert signal.action == SignalAction.BUY
    assert "suspected_vwap_low_reversal" in signal.rule_ids
    assert signal.needs_llm_review is True


def test_evaluate_signal_emits_suspected_sell_for_moderate_vwap_stretch() -> None:
    candles = series([10.0, 10.08, 10.16, 10.35, 10.45], [100, 110, 130, 150, 170])

    signal = evaluate_signal(candles, [], position(), fresh_health(), now=datetime(2026, 6, 5, 10, 5))

    assert signal.kind == SignalKind.SUSPECTED
    assert signal.action == SignalAction.SELL
    assert "suspected_vwap_high_stretch" in signal.rule_ids


def test_evaluate_signal_suppresses_suspected_sell_when_sector_strong_and_stock_pulls_back() -> None:
    session = series(
        [100, 103, 106, 109, 107, 106.5],
        [1000, 1200, 1400, 1600, 1300, 1200],
    )
    market_context = MarketContext(
        index_change_pct=0,
        sector_change_pct=6.0,
        sector_relative_strength_pct=6.0,
        stock_vs_sector_pct=0.4,
        sector_trend="up",
        index_trend="unknown",
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
        market_context=market_context,
    )

    assert signal.kind == SignalKind.HOLD


def test_evaluate_signal_adds_relative_weakness_sell_bias() -> None:
    candles = series([10.0, 10.0, 10.0, 10.0, 10.0], [100, 100, 100, 100, 100])
    index = [
        Candle(
            symbol="000001",
            timestamp=candle.timestamp,
            open=3000,
            high=3040,
            low=2990,
            close=3000 + index * 10,
            volume=1000,
        )
        for index, candle in enumerate(candles)
    ]

    signal = evaluate_signal(candles, index, position(), fresh_health(), now=datetime(2026, 6, 5, 10, 5))

    assert signal.kind == SignalKind.CANDIDATE_SELL
    assert signal.action == SignalAction.SELL
    assert "relative_weakness" in signal.rule_ids


def test_evaluate_signal_holds_when_provider_data_is_stale() -> None:
    candles = series([10.5, 10.2, 9.9, 9.6, 9.55], [500, 420, 340, 260, 180])
    stale = ProviderHealth(
        provider="akshare",
        symbol="600000",
        last_success_at=datetime(2026, 6, 5, 10, 1),
        stale_after_seconds=90,
    )

    signal = evaluate_signal(candles, [], position(), stale, now=datetime(2026, 6, 5, 10, 5))

    assert signal.kind == SignalKind.HOLD
    assert signal.action == SignalAction.HOLD
    assert signal.source_fresh is False
    assert "数据延迟" in signal.reason


def test_compute_indicators_stays_available_for_rule_inputs() -> None:
    candles = series([10.0, 10.1, 10.2], [100, 110, 120])

    snapshot = compute_indicators(candles)

    assert snapshot.vwap > 0
