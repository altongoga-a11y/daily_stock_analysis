# -*- coding: utf-8 -*-
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.schemas.us_market_intelligence import (
    DataQualitySummary,
    QuoteFact,
    QuoteProvenance,
    USMarketIntelligenceSnapshot,
)
from src.services.us_market_intelligence import (
    NewsItem,
    SECTOR_SYMBOLS,
    fetch_us_market_snapshot,
    market_session,
    validate_news_item,
)
from src.services.us_market_intelligence_anomalies import detect_market_anomalies
from src.services.us_market_intelligence_comparison import compare_with_baselines
from src.services.us_market_intelligence_engine import run_intelligence_engine
from src.services.us_market_intelligence_history import USMarketIntelligenceHistory

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _quote(symbol, price, change, generated, *, status="valid"):
    return QuoteFact(
        evidence_id=f"quote-{symbol}",
        symbol=symbol,
        name=symbol,
        price=price,
        change_pct=change,
        previous_close=price / (1 + change / 100) if change is not None else None,
        stale_seconds=0,
        provenance=QuoteProvenance(
            provider="Yahoo Finance",
            provider_timestamp=generated,
            retrieved_at=generated,
            market_time=generated,
            market_session="intraday",
            ttl_seconds=900,
            validation_status=status,
        ),
    )


def _snapshot(snapshot_id, generated, changes=None):
    changes = changes or {"SPY": 0.2, "^VIX": -1.0, "XLK": 0.4, "XLF": 0.1}
    return USMarketIntelligenceSnapshot(
        snapshot_id=snapshot_id,
        generated_at=generated.astimezone(UTC).isoformat(),
        generated_at_new_york=generated.astimezone(NY).isoformat(),
        market_session="intraday",
        session_label="盘中",
        quotes=[_quote(symbol, 100 + index, change, generated.astimezone(UTC).isoformat()) for index, (symbol, change) in enumerate(changes.items())],
        news=[],
        data_quality=DataQualitySummary(
            quote_coverage_pct=100,
            fresh_quote_pct=100,
            sector_etf_coverage_pct=100,
            news_total=0,
            news_validated=0,
            news_with_published_at=0,
            overall_status="good",
        ),
    )


def _fake_download(symbols, *, interval, **_kwargs):
    tickers = list(symbols)
    now = pd.Timestamp("2026-11-19 12:00:00", tz=NY)
    if interval == "5m":
        index = pd.DatetimeIndex([now - pd.Timedelta(minutes=5), now])
        frames = {symbol: pd.DataFrame({"Close": [100, 101]}, index=index) for symbol in tickers}
    else:
        index = pd.date_range(end=now.normalize() - pd.Timedelta(days=1), periods=160, freq="B", tz=NY)
        frames = {
            symbol: pd.DataFrame({"Close": [90 + row * 0.0625 for row in range(160)]}, index=index)
            for symbol in tickers
        }
    return pd.concat(frames, axis=1)


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


def test_news_validation_checks_url_reachability_and_publication_time():
    now = datetime(2026, 11, 19, 17, 0, tzinfo=UTC)
    item = NewsItem(
        category="财报",
        title="Company reports earnings",
        url="https://example.com/earnings",
        source="example.com",
        published_date="2026-11-19T16:00:00Z",
    )
    validated = validate_news_item(item, now=now, request=lambda *args, **kwargs: _Response(200))
    assert validated.validation_status == "valid"
    assert validated.http_status == 200
    assert validated.published_date == "2026-11-19T16:00:00+00:00"

    invalid = validate_news_item(
        NewsItem("热点", "Bad", "javascript:alert(1)", "unknown", "2026-11-20T21:00:00Z"),
        now=now,
    )
    assert invalid.validation_status == "invalid"
    assert "invalid_url" in invalid.validation_reasons
    assert "published_at_in_future" in invalid.validation_reasons


def test_news_source_is_derived_from_url_and_duplicate_titles_can_be_normalized():
    from src.services.us_market_intelligence import _canonical_news_title, _clean_news_result

    result = SimpleNamespace(
        title="Same Market Headline",
        url="https://reuters.com/markets/story",
        source="wrong-source.example",
        published_date="2026-11-19",
        snippet="summary",
    )
    cleaned = _clean_news_result(result, "市场主线", "2026-11-19T17:00:00+00:00")
    assert cleaned.source == "reuters.com"
    assert _canonical_news_title("Same—Market Headline!") == _canonical_news_title("same market headline")


def test_market_session_covers_premarket_intraday_postmarket_and_weekend(monkeypatch):
    class _Calendar:
        @staticmethod
        def is_session(value):
            return pd.Timestamp(value).day != 21

        @staticmethod
        def session_open(value):
            return pd.Timestamp(value, tz=NY) + pd.Timedelta(hours=9, minutes=30)

        @staticmethod
        def session_close(value):
            return pd.Timestamp(value, tz=NY) + pd.Timedelta(hours=16)

    monkeypatch.setattr("exchange_calendars.get_calendar", lambda _name: _Calendar())
    assert market_session(datetime(2026, 11, 19, 8, 0, tzinfo=NY)) == "premarket"
    assert market_session(datetime(2026, 11, 19, 10, 0, tzinfo=NY)) == "intraday"
    assert market_session(datetime(2026, 11, 19, 16, 5, tzinfo=NY)) == "postmarket"
    assert market_session(datetime(2026, 11, 21, 10, 0, tzinfo=NY)) == "non_trading"


def test_postmarket_completed_quotes_use_overnight_ttl():
    now = datetime(2026, 11, 19, 21, 7, tzinfo=NY)
    quotes, _warnings = fetch_us_market_snapshot(["AAPL"], now=now, download=_fake_download)
    assert quotes["AAPL"].market_session == "postmarket"
    assert quotes["AAPL"].ttl_seconds == 18 * 60 * 60
    assert quotes["AAPL"].validation_status == "valid"


def test_multihorizon_returns_are_collected_for_sector_rotation():
    quotes, _warnings = fetch_us_market_snapshot(
        ["AAPL"],
        now=datetime(2026, 11, 19, 12, 0, tzinfo=NY),
        download=_fake_download,
    )
    assert set(quotes["XLK"].horizon_returns) == {"1d", "5d", "1m", "3m", "6m"}
    expected_1d = round((101.0 - (90 + 159 * 0.0625)) / (90 + 159 * 0.0625) * 100, 2)
    assert quotes["XLK"].horizon_returns["1d"] == expected_1d
    assert quotes["XLK"].horizon_returns["6m"] is not None


def test_market_session_respects_half_day_close(monkeypatch):
    class _HalfDayCalendar:
        @staticmethod
        def is_session(_value):
            return True

        @staticmethod
        def session_open(value):
            return pd.Timestamp(value, tz=NY) + pd.Timedelta(hours=9, minutes=30)

        @staticmethod
        def session_close(value):
            return pd.Timestamp(value, tz=NY) + pd.Timedelta(hours=13)

    monkeypatch.setattr("exchange_calendars.get_calendar", lambda _name: _HalfDayCalendar())
    assert market_session(datetime(2026, 11, 27, 12, 30, tzinfo=NY)) == "intraday"
    assert market_session(datetime(2026, 11, 27, 13, 15, tzinfo=NY)) == "postmarket"


def test_snapshot_schema_rejects_unknown_future_version():
    snapshot = _snapshot("versioned", datetime(2026, 11, 19, 12, 0, tzinfo=NY))
    payload = snapshot.to_json_dict()
    payload["schema_version"] = "2.0"
    with pytest.raises(ValueError):
        USMarketIntelligenceSnapshot.model_validate(payload)


def test_history_round_trip_and_baselines_skip_corrupt_snapshot(tmp_path):
    history = USMarketIntelligenceHistory(tmp_path)
    day_one = datetime(2026, 11, 18, 16, 15, tzinfo=NY)
    day_two = datetime(2026, 11, 19, 11, 15, tzinfo=NY)
    history.save(_snapshot("day-one", day_one))
    history.save(_snapshot("hour-one", day_two))
    corrupt = tmp_path / "snapshots" / "2026-11-19" / "corrupt.json"
    corrupt.write_text("{not-json", encoding="utf-8")

    previous_hour, previous_day = history.baselines(datetime(2026, 11, 19, 12, 15, tzinfo=NY))
    assert previous_hour.snapshot_id == "hour-one"
    assert previous_day.snapshot_id == "day-one"
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["snapshot_id"] == "hour-one"


def test_comparison_and_anomalies_detect_vix_jump_leadership_and_stale_data():
    previous_time = datetime(2026, 11, 19, 10, 15, tzinfo=NY)
    current_time = previous_time + timedelta(hours=1)
    previous = _snapshot("previous", previous_time, {"SPY": 0.1, "^VIX": 0.0, "XLK": 1.0, "XLF": 0.1})
    current = _snapshot("current", current_time, {"SPY": 0.2, "^VIX": 5.0, "XLK": 0.2, "XLF": 2.0})
    next(item for item in current.quotes if item.symbol == "^VIX").price = 110.0
    current.quotes[0].provenance.validation_status = "stale"
    current.comparison = compare_with_baselines(
        current,
        previous_hour=previous,
        previous_trading_day=None,
        sector_symbols=SECTOR_SYMBOLS,
    )
    anomalies = detect_market_anomalies(current, sector_symbols=SECTOR_SYMBOLS)
    types = {item.anomaly_type for item in anomalies}
    assert "vix_jump" in types
    assert "sector_leadership_reversal" in types
    assert "stale_or_missing_data" in types
    assert any(item.evidence_ids for item in anomalies)


def test_workflow_persists_state_and_exports_markdown_and_json():
    workflow = (Path(__file__).parents[1] / ".github/workflows/05-us-market-intelligence.yml").read_text(encoding="utf-8")
    assert "恢复最近市场快照" in workflow
    assert "us-market-intelligence-state" in workflow
    assert "reports/us-market-intelligence.md" in workflow
    assert "reports/us-market-intelligence.json" in workflow
    assert "data/us-market-intelligence/" in workflow


def test_engine_emits_same_snapshot_id_in_markdown_json_contract_and_history(tmp_path):
    config = SimpleNamespace(stock_list=["AAPL"], brave_api_keys=[])
    result = run_intelligence_engine(
        config,
        watchlist=["AAPL"],
        now=datetime(2026, 11, 19, 12, 0, tzinfo=NY),
        download=_fake_download,
        generate=lambda _prompt: ("【数据推断】风险偏好温和。", "test", "model"),
        history=USMarketIntelligenceHistory(tmp_path),
        validate_news_links=False,
    )
    payload = result.snapshot.to_json_dict()
    assert result.snapshot.snapshot_id in result.report
    assert payload["snapshot_id"] == result.snapshot.snapshot_id
    assert result.history_path.exists()
    assert len([item for item in result.snapshot.quotes if item.symbol in SECTOR_SYMBOLS]) == 11
    assert all(item.provenance.provider == "Yahoo Finance" for item in result.snapshot.quotes)
    assert result.snapshot.intelligence.status == "generated"
    assert result.report.index("经济周期") < result.report.index("数据附录：核心市场")
    assert "多周期行业轮动" in result.report
    assert "未来1个交易日" in result.report
    assert "未来1个月" in result.report
    assert "未来3个月" in result.report
    assert "未来6个月" in result.report
    assert "值得关注的相对强势股票" in result.report
