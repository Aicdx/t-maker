from datetime import datetime, timedelta

from tmaker.domain.models import Candle, Position, ProviderHealth, SignalAction, SignalKind
from tmaker.strategy.indicators import compute_indicators
from tmaker.strategy.rules import MarketContext, evaluate_signal


def series(closes: list[float], volumes: list[float], symbol: str = "600000") -> list[Candle]:
    start = datetime(2026, 6, 5, 10, 1)
    return [
        Candle(
            symbol=symbol,
            timestamp=start + timedelta(minutes=index),
            open=close + 0.05,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]


def opening_series(closes: list[float], volumes: list[float]) -> list[Candle]:
    start = datetime(2026, 6, 5, 9, 30)
    return [
        Candle(
            symbol="600000",
            timestamp=start + timedelta(minutes=index),
            open=closes[0] if index == 0 else close,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]


def timed_series(closes: list[float], volumes: list[float], minute_offsets: list[int]) -> list[Candle]:
    start = datetime(2026, 6, 5, 10, 1)
    return [
        Candle(
            symbol="600000",
            timestamp=start + timedelta(minutes=minute_offsets[index]),
            open=close + 0.05,
            high=close + 0.1,
            low=close - 0.1,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]


def ohlcv_series(
    rows: list[tuple[float, float, float, float, float]],
    symbol: str = "600000",
) -> list[Candle]:
    start = datetime(2026, 6, 5, 10, 1)
    return [
        Candle(
            symbol=symbol,
            timestamp=start + timedelta(minutes=index),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
        )
        for index, (open_price, high, low, close, volume) in enumerate(rows)
    ]


def position(symbol: str = "600000") -> Position:
    return Position(symbol=symbol, base_quantity=1000, cost_price=10, available_cash=10000, t_quantity=300)


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


def test_evaluate_signal_emits_sell_for_rally_fade_before_turning_negative() -> None:
    session = series(
        [10.30, 10.60, 10.95, 10.38, 10.25],
        [5000, 7800, 9200, 6600, 6100],
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert signal.kind == SignalKind.CANDIDATE_SELL
    assert signal.action == SignalAction.SELL
    assert "rally_fade_sell" in signal.rule_ids
    assert "break_open_after_rally" in signal.rule_ids
    assert "冲高回落" in signal.reason


def test_evaluate_signal_does_not_repeat_rally_fade_after_open_break_already_triggered() -> None:
    session = series(
        [10.30, 10.60, 10.95, 10.25, 10.18],
        [5000, 7800, 9200, 6100, 5900],
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "rally_fade_sell" not in signal.rule_ids
    assert "break_open_after_rally" not in signal.rule_ids


def test_evaluate_signal_ignores_stale_rally_fade_long_after_intraday_high() -> None:
    session = timed_series(
        [10.30, 10.60, 10.95, 10.40, 10.30],
        [5000, 7800, 9200, 6600, 6100],
        [0, 1, 2, 130, 131],
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "rally_fade_sell" not in signal.rule_ids
    assert "break_vwap_after_rally" not in signal.rule_ids


def test_evaluate_signal_uses_t_stop_instead_of_buy_when_opening_downtrend_breaks_open() -> None:
    session = opening_series(
        [10.00, 9.98, 9.92, 9.80, 9.74],
        [500, 420, 340, 260, 180],
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert signal.kind == SignalKind.CANDIDATE_SELL
    assert signal.action == SignalAction.SELL
    assert "opening_downtrend_t_stop" in signal.rule_ids
    assert "sharp_drop_shrinking_volume" not in signal.rule_ids
    assert "减仓" in signal.reason


def test_evaluate_signal_does_not_repeat_opening_downtrend_after_bottom_line_already_triggered() -> None:
    session = opening_series(
        [10.00, 9.74, 9.68, 9.61, 9.55, 9.48],
        [500, 420, 340, 260, 180, 160],
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "opening_downtrend_t_stop" not in signal.rule_ids


def test_evaluate_signal_emits_limit_break_only_when_fade_threshold_is_crossed() -> None:
    session = series(
        [10.00, 10.98, 10.78, 10.62],
        [5000, 12000, 9000, 8800],
    )

    signal = evaluate_signal(
        session[-4:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "limit_break_fade" in signal.rule_ids


def test_evaluate_signal_does_not_repeat_limit_break_after_fade_threshold_already_triggered() -> None:
    session = series(
        [10.00, 10.98, 10.62, 10.50],
        [5000, 12000, 8800, 8500],
    )

    signal = evaluate_signal(
        session[-4:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "limit_break_fade" not in signal.rule_ids


def test_evaluate_signal_ignores_stale_limit_break_long_after_intraday_high() -> None:
    session = timed_series(
        [10.00, 10.98, 10.78, 10.62],
        [5000, 12000, 9000, 8800],
        [0, 1, 130, 131],
    )

    signal = evaluate_signal(
        session[-4:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "limit_break_fade" not in signal.rule_ids


def test_evaluate_signal_emits_sell_when_weak_rebound_fails_at_session_vwap() -> None:
    session = series(
        [1182.0, 1145.0, 1140.0, 1161.1, 1156.0],
        [1000, 900, 850, 800, 760],
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert signal.kind == SignalKind.CANDIDATE_SELL
    assert signal.action == SignalAction.SELL
    assert "weak_rebound_session_vwap_fail" in signal.rule_ids
    assert "反抽均价失败" in signal.reason


def test_evaluate_signal_delays_buy_when_pullback_is_too_close_to_session_vwap() -> None:
    session = series(
        [100.0, 101.0, 101.5, 100.2, 99.4, 99.1, 99.3],
        [1000, 900, 850, 800, 760, 700, 650],
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "pullback_low_rebound" not in signal.rule_ids


def test_evaluate_signal_emits_buy_when_price_is_far_below_session_vwap_near_low() -> None:
    session = series(
        [1182.0, 1160.0, 1150.0, 1140.0, 1128.8, 1129.0],
        [1000, 900, 850, 800, 760, 740],
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert signal.kind == SignalKind.SUSPECTED
    assert signal.action == SignalAction.BUY
    assert "deep_session_vwap_low_buy" in signal.rule_ids
    assert "远离全天均价" in signal.reason


def test_evaluate_signal_does_not_repeat_deep_session_vwap_low_buy_without_new_low() -> None:
    session = series(
        [1182.0, 1160.0, 1150.0, 1128.8, 1129.0, 1128.7, 1128.9],
        [1000, 900, 850, 800, 760, 740, 720],
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "deep_session_vwap_low_buy" not in signal.rule_ids


def test_evaluate_signal_keeps_lunch_reopen_low_retest_buy() -> None:
    session = timed_series(
        [1182.0, 1160.0, 1150.0, 1128.8, 1129.0],
        [1000, 900, 850, 800, 760],
        [0, 30, 60, 89, 180],
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position(),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "deep_session_vwap_low_buy" in signal.rule_ids


def test_evaluate_signal_emits_suspected_buy_for_moderate_drop_below_vwap() -> None:
    candles = series([10.5, 10.35, 10.18, 10.02, 9.95], [500, 470, 440, 410, 390])

    signal = evaluate_signal(candles, [], position(), fresh_health(), now=datetime(2026, 6, 5, 10, 5))

    assert signal.kind == SignalKind.SUSPECTED
    assert signal.action == SignalAction.BUY
    assert "suspected_vwap_low_reversal" in signal.rule_ids
    assert signal.needs_llm_review is True


def test_evaluate_signal_emits_suspected_buy_for_low_pullback_rebound() -> None:
    session = series(
        [100.0, 104.0, 108.0, 107.0, 105.8, 104.6, 104.9],
        [100, 400, 2000, 1600, 1200, 900, 700],
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

    assert signal.kind == SignalKind.SUSPECTED
    assert signal.action == SignalAction.BUY
    assert "pullback_low_rebound" in signal.rule_ids
    assert signal.needs_llm_review is True


def test_evaluate_signal_blocks_300502_pullback_buy_after_deep_fade_from_session_high() -> None:
    session = series(
        [552.0, 574.0, 562.0, 548.0, 536.0, 529.6, 530.87],
        [5000, 8000, 6200, 5000, 4300, 3600, 2600],
        symbol="300502",
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position("300502"),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "pullback_low_rebound" not in signal.rule_ids
    assert "deep_session_vwap_low_buy" not in signal.rule_ids
    assert signal.kind == SignalKind.HOLD


def test_evaluate_signal_keeps_300502_extreme_deep_rebound_buy() -> None:
    session = ohlcv_series(
        [
            (552.0, 552.0, 552.0, 552.0, 5000),
            (552.0, 574.04, 552.0, 574.0, 8000),
            (574.0, 574.0, 562.0, 562.0, 6200),
            (562.0, 562.0, 548.0, 548.0, 5000),
            (548.0, 548.0, 536.0, 536.0, 4300),
            (536.0, 536.0, 529.6, 529.6, 5026),
            (529.6, 529.6, 520.0, 520.0, 6000),
            (520.0, 520.0, 512.0, 512.0, 7000),
            (510.12, 510.12, 510.01, 510.01, 3662),
            (510.01, 510.01, 507.4, 507.4, 7510),
            (507.4, 507.4, 506.45, 506.45, 5767),
            (506.45, 506.45, 504.1, 504.1, 5397),
            (504.1, 504.1, 502.0, 502.0, 7715),
            (502.0, 502.0, 499.5, 499.5, 14390),
            (499.5, 503.44, 499.5, 503.44, 7748),
        ],
        symbol="300502",
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position("300502"),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert signal.kind == SignalKind.SUSPECTED
    assert signal.action == SignalAction.BUY
    assert "deep_session_vwap_low_buy" in signal.rule_ids


def test_evaluate_signal_blocks_300502_pullback_buy_when_discount_is_too_shallow() -> None:
    session = series(
        [641.0, 638.0, 634.0, 629.0, 625.0, 621.0, 621.76, 621.49, 624.0],
        [5000, 4000, 3500, 3000, 2500, 2961, 1570, 1839, 1065],
        symbol="300502",
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position("300502"),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "pullback_low_rebound" not in signal.rule_ids
    assert signal.kind == SignalKind.HOLD


def test_evaluate_signal_keeps_300502_pullback_buy_when_fade_is_not_deep() -> None:
    session = series(
        [596.0, 598.0, 600.0, 599.0, 596.0, 592.0, 589.0, 587.0, 584.48, 586.4],
        [4000, 4200, 4500, 3800, 3200, 2600, 2300, 2100, 1900, 1600],
        symbol="300502",
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position("300502"),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "pullback_low_rebound" in signal.rule_ids


def test_evaluate_signal_keeps_other_symbols_with_300502_fade_profile() -> None:
    session = series(
        [552.0, 574.0, 562.0, 548.0, 536.0, 529.6, 530.87],
        [5000, 8000, 6200, 5000, 4300, 3600, 2600],
        symbol="300308",
    )

    signal = evaluate_signal(
        session[-5:],
        [],
        position("300308"),
        fresh_health_at(session[-1].timestamp),
        now=session[-1].timestamp,
        session_candles=session,
    )

    assert "pullback_low_rebound" in signal.rule_ids or "deep_session_vwap_low_buy" in signal.rule_ids


def test_evaluate_signal_emits_suspected_buy_for_midday_low_rebound() -> None:
    session = series(
        [100.0, 101.8, 103.4, 104.5, 104.3, 103.7, 101.5, 100.5, 99.8, 99.95],
        [500, 700, 1000, 1800, 1600, 1300, 1100, 900, 1900, 900],
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

    assert signal.kind == SignalKind.SUSPECTED
    assert signal.action == SignalAction.BUY
    assert "deep_session_vwap_low_buy" in signal.rule_ids


def test_evaluate_signal_does_not_emit_buy_while_price_is_falling_on_expanding_volume() -> None:
    candles = series([10.8, 10.7, 10.55, 10.35, 10.15], [100, 120, 150, 190, 240])

    signal = evaluate_signal(candles, [], position(), fresh_health(), now=datetime(2026, 6, 5, 10, 5))

    assert signal.kind == SignalKind.HOLD
    assert signal.action == SignalAction.HOLD


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
