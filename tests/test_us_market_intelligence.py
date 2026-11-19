# -*- coding: utf-8 -*-
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from src.services.us_market_intelligence import (
    SECTOR_SYMBOLS,
    USMarketIntelligenceData,
    QuotePoint,
    build_prompt,
    compose_report,
    fetch_us_market_snapshot,
    normalize_watchlist,
    should_run_scheduled,
)

NY = ZoneInfo("America/New_York")


def _fake_download(symbols, *, interval, **_kwargs):
    tickers = list(symbols)
    now = pd.Timestamp("2026-11-19 12:00:00", tz=NY)
    if interval == "5m":
        index = pd.DatetimeIndex([now - pd.Timedelta(minutes=5), now])
        frames = {
            symbol: pd.DataFrame({"Close": [100 + pos, 102 + pos]}, index=index)
            for pos, symbol in enumerate(tickers)
        }
    else:
        index = pd.DatetimeIndex([now.normalize() - pd.Timedelta(days=2), now.normalize() - pd.Timedelta(days=1)])
        frames = {
            symbol: pd.DataFrame({"Close": [98 + pos, 100 + pos]}, index=index)
            for pos, symbol in enumerate(tickers)
        }
    return pd.concat(frames, axis=1)


def test_normalize_watchlist_deduplicates_and_converts_class_share_separator():
    assert normalize_watchlist("AAPL, msft,BRK.B,AAPL, bad ticker") == ["AAPL", "MSFT", "BRK-B"]


def test_fetch_snapshot_computes_change_from_previous_regular_close():
    now = datetime(2026, 11, 19, 12, 3, tzinfo=NY)
    quotes, warnings = fetch_us_market_snapshot(["AAPL"], now=now, download=_fake_download)

    assert warnings == []
    assert quotes["AAPL"].price is not None
    assert quotes["AAPL"].change_pct is not None
    assert quotes["AAPL"].stale_minutes == 3
    assert set(SECTOR_SYMBOLS).issubset(quotes)


def test_snapshot_keeps_daily_failure_visible_without_raising():
    def partial_download(symbols, *, interval, **kwargs):
        if interval == "1d":
            raise TimeoutError("daily unavailable")
        return _fake_download(symbols, interval=interval, **kwargs)

    quotes, warnings = fetch_us_market_snapshot(
        ["AAPL"],
        now=datetime(2026, 11, 19, 12, 3, tzinfo=NY),
        download=partial_download,
    )

    assert "AAPL" in quotes
    assert quotes["AAPL"].change_pct is None
    assert any("日线基准抓取失败" in warning for warning in warnings)


def test_schedule_gate_uses_new_york_hours(monkeypatch):
    class _Calendar:
        @staticmethod
        def is_session(_date):
            return True

    monkeypatch.setattr("exchange_calendars.get_calendar", lambda _name: _Calendar())

    allowed, label = should_run_scheduled(datetime(2026, 11, 19, 8, 15, tzinfo=NY))
    assert allowed is True
    assert label == "盘前"

    allowed, reason = should_run_scheduled(datetime(2026, 11, 19, 18, 15, tzinfo=NY))
    assert allowed is False
    assert "不在" in reason


def test_report_and_prompt_disclose_data_boundaries():
    data = USMarketIntelligenceData(
        generated_at=datetime(2026, 11, 19, 12, 0, tzinfo=NY),
        session_label="盘中",
        quotes={
            "SPY": QuotePoint("SPY", "S&P 500 ETF", 600.0, 1.0, "2026-11-19T17:00:00+00:00", 0),
            "XLK": QuotePoint("XLK", "科技", 250.0, 2.0, "2026-11-19T17:00:00+00:00", 0),
        },
        warnings=["测试告警"],
    )

    prompt = build_prompt(data)
    report = compose_report(data, "## 市场状态\n\n科技领涨。")

    assert "禁止编造" in prompt
    assert "相对 SPY" in prompt
    assert "Yahoo Finance" in report
    assert "不是交易所直连报价" in report
    assert "测试告警" in report
