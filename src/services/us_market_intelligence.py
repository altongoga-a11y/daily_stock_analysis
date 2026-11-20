# -*- coding: utf-8 -*-
"""Hourly US market intelligence assembled from market snapshots and fresh news."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dateutil import parser as date_parser

from src.schemas.us_market_intelligence import (
    AIIntelligence,
    DataQualitySummary,
    IntelligenceClaim,
    MonitorTrigger,
    NewsEvidence,
    QuoteFact,
    QuoteProvenance,
    USMarketIntelligenceSnapshot,
)

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

MARKET_SYMBOLS: dict[str, str] = {
    "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq 100 ETF",
    "DIA": "Dow Jones ETF",
    "IWM": "Russell 2000 ETF",
    "^VIX": "VIX",
}

SECTOR_SYMBOLS: dict[str, str] = {
    "XLK": "科技",
    "XLF": "金融",
    "XLY": "可选消费",
    "XLC": "通信服务",
    "XLI": "工业",
    "XLV": "医疗保健",
    "XLE": "能源",
    "XLP": "必选消费",
    "XLU": "公用事业",
    "XLRE": "房地产",
    "XLB": "原材料",
}

MACRO_SYMBOLS: dict[str, str] = {
    "^TNX": "美国10年期国债收益率",
    "DX-Y.NYB": "美元指数",
    "GC=F": "黄金期货",
    "CL=F": "WTI原油期货",
}

NEWS_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "市场主线",
        "US stock market breaking news today S&P 500 Nasdaq Federal Reserve rates inflation jobs Treasury",
    ),
    (
        "板块轮动",
        "US stock market sector rotation today technology financial energy healthcare industrial ETF",
    ),
    (
        "经济日历",
        "US economic calendar today CPI PPI payrolls jobless claims GDP Fed speakers Treasury auction",
    ),
    (
        "财报",
        "US earnings today results guidance revenue EPS premarket after hours large cap companies",
    ),
)


@dataclass(frozen=True)
class QuotePoint:
    symbol: str
    name: str
    price: Optional[float]
    change_pct: Optional[float]
    as_of: Optional[str]
    stale_minutes: Optional[int]
    source: str = "Yahoo Finance"
    retrieved_at: Optional[str] = None
    market_session: str = "unknown"
    ttl_seconds: int = 900
    validation_status: str = "unverified"
    validation_reasons: tuple[str, ...] = ()
    previous_close: Optional[float] = None
    horizon_returns: dict[str, Optional[float]] = field(default_factory=dict)


@dataclass(frozen=True)
class NewsItem:
    category: str
    title: str
    url: str
    source: str
    published_date: Optional[str] = None
    snippet: str = ""
    retrieved_at: Optional[str] = None
    validation_status: str = "unverified"
    validation_reasons: tuple[str, ...] = ()
    http_status: Optional[int] = None


@dataclass
class USMarketIntelligenceData:
    generated_at: datetime
    session_label: str
    quotes: dict[str, QuotePoint] = field(default_factory=dict)
    news: list[NewsItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def sectors(self) -> list[QuotePoint]:
        rows = [self.quotes[symbol] for symbol in SECTOR_SYMBOLS if symbol in self.quotes]
        return sorted(rows, key=lambda item: _sort_number(item.change_pct), reverse=True)

    @property
    def watchlist(self) -> list[QuotePoint]:
        known = set(MARKET_SYMBOLS) | set(SECTOR_SYMBOLS) | set(MACRO_SYMBOLS)
        rows = [quote for symbol, quote in self.quotes.items() if symbol not in known]
        return sorted(rows, key=lambda item: abs(_sort_number(item.change_pct)), reverse=True)


def _sort_number(value: Optional[float]) -> float:
    if value is None or not math.isfinite(value):
        return -1e12
    return float(value)


def _clean_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def normalize_watchlist(raw: str | Iterable[str]) -> list[str]:
    """Return unique Yahoo-compatible US ticker symbols while preserving order."""
    values = raw.split(",") if isinstance(raw, str) else list(raw)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value).strip().upper().replace(".", "-")
        if not symbol or symbol in seen:
            continue
        if not all(char.isalnum() or char in {"-", "^", "="} for char in symbol):
            logger.warning("Skipping unsupported US watchlist symbol: %s", value)
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def market_session(now: Optional[datetime] = None) -> str:
    current = (now or datetime.now(tz=NEW_YORK)).astimezone(NEW_YORK)
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XNYS")
        session = pd.Timestamp(current.date())
        if not calendar.is_session(session):
            return "non_trading"
        open_at = calendar.session_open(session).tz_convert(NEW_YORK).to_pydatetime()
        close_at = calendar.session_close(session).tz_convert(NEW_YORK).to_pydatetime()
        if current < open_at:
            return "premarket"
        if current < close_at:
            return "intraday"
        return "postmarket"
    except Exception:
        if current.weekday() >= 5:
            return "non_trading"

    minute = current.hour * 60 + current.minute
    if minute < 9 * 60 + 30:
        return "premarket"
    if minute < 16 * 60:
        return "intraday"
    return "postmarket"


def session_label(now: Optional[datetime] = None) -> str:
    labels = {
        "premarket": "盘前",
        "intraday": "盘中",
        "postmarket": "盘后",
        "non_trading": "休市",
        "unknown": "未知",
    }
    return labels[market_session(now)]


def should_run_scheduled(now: Optional[datetime] = None) -> tuple[bool, str]:
    """Gate hourly workflow runs by NYSE trading day and useful New York hours."""
    current = (now or datetime.now(tz=NEW_YORK)).astimezone(NEW_YORK)
    if current.weekday() >= 5:
        return False, "纽约时间为周末"

    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XNYS")
        if not calendar.is_session(pd.Timestamp(current.date())):
            return False, "NYSE 当日休市"
    except Exception as exc:
        logger.warning("NYSE calendar check failed open: %s", exc)

    # The workflow fires at :15. Cover 07:15-16:15 ET: two pre-market
    # snapshots, the opening-hour snapshot, hourly intraday updates and close.
    if current.hour not in range(7, 17):
        return False, "不在美股情报窗口（07:00-16:59 ET）"
    return True, session_label(current)


def _ticker_frame(data: pd.DataFrame, symbol: str, symbol_count: int) -> pd.DataFrame:
    if data is None or data.empty:
        return pd.DataFrame()
    if not isinstance(data.columns, pd.MultiIndex):
        return data.copy() if symbol_count == 1 else pd.DataFrame()

    level_zero = set(str(value) for value in data.columns.get_level_values(0))
    level_one = set(str(value) for value in data.columns.get_level_values(1))
    if symbol in level_zero:
        return data[symbol].copy()
    if symbol in level_one:
        return data.xs(symbol, axis=1, level=1).copy()
    return pd.DataFrame()


def _latest_close(frame: pd.DataFrame) -> tuple[Optional[float], Optional[pd.Timestamp]]:
    if frame.empty or "Close" not in frame:
        return None, None
    closes = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if closes.empty:
        return None, None
    timestamp = pd.Timestamp(closes.index[-1])
    return _clean_float(closes.iloc[-1]), timestamp


HORIZON_TRADING_DAYS: dict[str, int] = {
    "1d": 1,
    "5d": 5,
    "1m": 21,
    "3m": 63,
    "6m": 126,
}


def _horizon_returns(frame: pd.DataFrame, latest_price: Optional[float]) -> dict[str, Optional[float]]:
    returns: dict[str, Optional[float]] = {key: None for key in HORIZON_TRADING_DAYS}
    if frame.empty or "Close" not in frame or latest_price is None:
        return returns
    closes = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if closes.empty:
        return returns
    for label, sessions in HORIZON_TRADING_DAYS.items():
        # Daily history normally ends at the previous completed session while
        # latest_price is intraday/after-hours, so 1d uses the last daily close.
        index = -sessions
        if len(closes) < sessions:
            continue
        baseline = _clean_float(closes.iloc[index])
        if baseline not in (None, 0):
            returns[label] = round((latest_price - baseline) / baseline * 100, 2)
    return returns


def _previous_regular_close(frame: pd.DataFrame, now: datetime) -> Optional[float]:
    if frame.empty or "Close" not in frame:
        return None
    closes = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if closes.empty:
        return None

    index_dates = pd.DatetimeIndex(closes.index)
    if index_dates.tz is not None:
        index_dates = index_dates.tz_convert(NEW_YORK).tz_localize(None)
    previous = closes[index_dates.date < now.astimezone(NEW_YORK).date()]
    if not previous.empty:
        return _clean_float(previous.iloc[-1])
    return _clean_float(closes.iloc[-2]) if len(closes) >= 2 else None


def fetch_us_market_snapshot(
    watchlist: Iterable[str],
    *,
    now: Optional[datetime] = None,
    download: Optional[Callable[..., pd.DataFrame]] = None,
) -> tuple[dict[str, QuotePoint], list[str]]:
    """Fetch latest available intraday prices and prior closes in two bulk requests."""
    current = (now or datetime.now(tz=NEW_YORK)).astimezone(NEW_YORK)
    symbols = list(dict.fromkeys([*MARKET_SYMBOLS, *SECTOR_SYMBOLS, *MACRO_SYMBOLS, *watchlist]))
    names = {**MARKET_SYMBOLS, **SECTOR_SYMBOLS, **MACRO_SYMBOLS}
    names.update({symbol: symbol for symbol in watchlist})

    if download is None:
        import yfinance as yf

        download = yf.download

    warnings: list[str] = []
    try:
        intraday = download(
            symbols,
            period="5d",
            interval="5m",
            group_by="ticker",
            auto_adjust=True,
            prepost=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        intraday = pd.DataFrame()
        warnings.append(f"盘中行情抓取失败：{type(exc).__name__}")
        logger.warning("US intraday snapshot failed: %s", exc)

    try:
        daily = download(
            symbols,
            period="1y",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        daily = pd.DataFrame()
        warnings.append(f"日线基准抓取失败：{type(exc).__name__}")
        logger.warning("US daily baseline failed: %s", exc)

    quotes: dict[str, QuotePoint] = {}
    now_utc = current.astimezone(UTC)
    current_session = market_session(current)
    if current_session in {"premarket", "intraday"}:
        ttl_seconds = 15 * 60
    elif current_session == "postmarket":
        # After extended-hours data stops updating, the latest completed session
        # remains valid until the next premarket. Do not flag the whole market stale.
        ttl_seconds = 18 * 60 * 60
    else:
        ttl_seconds = 36 * 60 * 60
    retrieved_at = now_utc.isoformat()
    for symbol in symbols:
        intraday_frame = _ticker_frame(intraday, symbol, len(symbols))
        daily_frame = _ticker_frame(daily, symbol, len(symbols))
        price, timestamp = _latest_close(intraday_frame)
        previous_close = _previous_regular_close(daily_frame, current)
        horizon_returns = _horizon_returns(daily_frame, price)
        change_pct = None
        if price is not None and previous_close not in (None, 0):
            change_pct = round((price - previous_close) / previous_close * 100, 2)

        as_of = None
        stale_minutes = None
        validation_reasons: list[str] = []
        if timestamp is not None:
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize(UTC)
            timestamp_utc = timestamp.tz_convert(UTC)
            as_of = timestamp_utc.isoformat()
            stale_minutes = max(0, int((now_utc - timestamp_utc.to_pydatetime()).total_seconds() // 60))
        else:
            validation_reasons.append("provider_timestamp_missing")
        if price is None or price <= 0:
            validation_reasons.append("price_missing_or_invalid")
        if previous_close is None:
            validation_reasons.append("previous_close_missing")
        stale_seconds = stale_minutes * 60 if stale_minutes is not None else None
        validation_status = "valid"
        if price is None:
            validation_status = "missing"
        elif stale_seconds is None:
            validation_status = "unverified"
        elif stale_seconds > ttl_seconds:
            validation_status = "stale"
            validation_reasons.append("quote_ttl_exceeded")

        if price is not None:
            quotes[symbol] = QuotePoint(
                symbol=symbol,
                name=names[symbol],
                price=round(price, 4),
                change_pct=change_pct,
                as_of=as_of,
                stale_minutes=stale_minutes,
                retrieved_at=retrieved_at,
                market_session=current_session,
                ttl_seconds=ttl_seconds,
                validation_status=validation_status,
                validation_reasons=tuple(validation_reasons),
                previous_close=previous_close,
                horizon_returns=horizon_returns,
            )

    missing = [symbol for symbol in symbols if symbol not in quotes]
    if missing:
        warnings.append(f"缺失行情：{', '.join(missing[:12])}")
    return quotes, warnings


def _parse_published_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = date_parser.parse(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def validate_news_item(
    item: NewsItem,
    *,
    now: Optional[datetime] = None,
    request: Optional[Callable[..., Any]] = None,
) -> NewsItem:
    """Validate URL shape, reachability and publication time without trusting snippets."""
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    reasons: list[str] = []
    parsed_url = urlparse(item.url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        reasons.append("invalid_url")

    published = _parse_published_at(item.published_date)
    if published is None:
        reasons.append("published_at_missing_or_invalid")
    elif published > current + timedelta(hours=2):
        reasons.append("published_at_in_future")
    elif published < current - timedelta(days=3):
        reasons.append("published_at_expired")

    status_code = None
    if "invalid_url" not in reasons:
        requester = request or requests.head
        request_kwargs = {
            "allow_redirects": True,
            "timeout": 5,
            "headers": {"User-Agent": "daily-stock-analysis/US-market-intelligence"},
        }
        try:
            response = requester(item.url, **request_kwargs)
            status_code = int(response.status_code)
            if request is None and status_code in {403, 405}:
                response = requests.get(item.url, stream=True, **request_kwargs)
                status_code = int(response.status_code)
                response.close()
            if status_code >= 400:
                reasons.append(f"http_status_{status_code}")
        except requests.RequestException as exc:
            reasons.append(f"url_check_failed_{type(exc).__name__}")
        except Exception as exc:
            reasons.append(f"url_check_failed_{type(exc).__name__}")

    status = "valid" if not reasons else "unverified"
    if "invalid_url" in reasons or any(reason.startswith("http_status_") for reason in reasons):
        status = "invalid"
    elif "published_at_expired" in reasons:
        status = "stale"
    return NewsItem(
        category=item.category,
        title=item.title,
        url=item.url,
        source=item.source,
        published_date=published.isoformat() if published else item.published_date,
        snippet=item.snippet,
        retrieved_at=item.retrieved_at or current.isoformat(),
        validation_status=status,
        validation_reasons=tuple(reasons),
        http_status=status_code,
    )


def _canonical_news_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _clean_news_result(result: Any, category: str, retrieved_at: str) -> Optional[NewsItem]:
    url = (result.url or "").strip()
    title = re.sub(r"\s+", " ", (result.title or "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not title:
        return None
    domain = (parsed.hostname or "").lower().removeprefix("www.")
    # Reject malformed results where provider text has been appended to the URL.
    if url.endswith(tuple(str(index) for index in range(10))) and re.search(r"\)\d$", url):
        return None
    return NewsItem(
        category=category,
        title=title,
        url=url,
        source=domain or (result.source or "").strip(),
        published_date=result.published_date,
        snippet=re.sub(r"\s+", " ", (result.snippet or "")).strip()[:400],
        retrieved_at=retrieved_at,
    )


def collect_us_news(
    config: Any,
    *,
    now: Optional[datetime] = None,
    validate_links: bool = True,
    request: Optional[Callable[..., Any]] = None,
) -> tuple[list[NewsItem], list[str]]:
    """Collect and independently validate fresh US market news evidence."""
    warnings: list[str] = []
    keys = list(getattr(config, "brave_api_keys", []) or [])
    if not keys:
        return [], ["未配置 BRAVE_API_KEYS，新闻、经济日历和财报热点为空"]

    from src.search_service import BraveSearchProvider

    provider = BraveSearchProvider(keys)
    items: list[NewsItem] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for category, query in NEWS_QUERIES:
        try:
            response = provider.search(
                query,
                max_results=5,
                days=1,
                search_lang="en",
                country="US",
            )
        except Exception as exc:
            warnings.append(f"{category}新闻检索失败：{type(exc).__name__}")
            continue
        if not response.success:
            warnings.append(f"{category}新闻检索失败：{response.error_message or 'unknown error'}")
            continue
        retrieved_at = (now or datetime.now(tz=UTC)).astimezone(UTC).isoformat()
        for result in response.results:
            item = _clean_news_result(result, category, retrieved_at)
            if item is None:
                continue
            title_key = _canonical_news_title(item.title)
            if item.url in seen_urls or title_key in seen_titles:
                continue
            seen_urls.add(item.url)
            seen_titles.add(title_key)
            items.append(item)
    if validate_links and items:
        with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
            items = list(pool.map(lambda item: validate_news_item(item, now=now, request=request), items))
    invalid_count = sum(item.validation_status != "valid" for item in items)
    if invalid_count:
        warnings.append(f"{invalid_count} 条新闻未通过链接或发布时间完整校验")
    return items, warnings


def _evidence_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def build_structured_snapshot(data: USMarketIntelligenceData) -> USMarketIntelligenceSnapshot:
    """Convert collected dataclasses into the strict, versioned AI-consumable contract."""
    generated_utc = data.generated_at.astimezone(UTC)
    quote_facts = []
    for quote in data.quotes.values():
        stale_seconds = quote.stale_minutes * 60 if quote.stale_minutes is not None else None
        quote_facts.append(
            QuoteFact(
                evidence_id=_evidence_id("quote", f"{quote.symbol}|{quote.as_of}|{quote.price}"),
                symbol=quote.symbol,
                name=quote.name,
                price=quote.price,
                change_pct=quote.change_pct,
                previous_close=quote.previous_close,
                horizon_returns=dict(quote.horizon_returns),
                stale_seconds=stale_seconds,
                provenance=QuoteProvenance(
                    provider=quote.source,
                    provider_timestamp=quote.as_of,
                    retrieved_at=quote.retrieved_at or generated_utc.isoformat(),
                    market_time=(
                        datetime.fromisoformat(quote.as_of).astimezone(NEW_YORK).isoformat()
                        if quote.as_of
                        else None
                    ),
                    market_session=quote.market_session,
                    ttl_seconds=quote.ttl_seconds,
                    validation_status=quote.validation_status,
                    validation_reasons=list(quote.validation_reasons),
                ),
            )
        )

    news_evidence = [
        NewsEvidence(
            evidence_id=_evidence_id("news", item.url),
            category=item.category,
            title=item.title,
            url=item.url,
            source=item.source,
            published_at=item.published_date,
            retrieved_at=item.retrieved_at or generated_utc.isoformat(),
            snippet=item.snippet,
            validation_status=item.validation_status,
            validation_reasons=list(item.validation_reasons),
            http_status=item.http_status,
        )
        for item in data.news
    ]

    expected_count = len(MARKET_SYMBOLS) + len(SECTOR_SYMBOLS) + len(MACRO_SYMBOLS) + len(data.watchlist)
    valid_count = sum(item.provenance.validation_status == "valid" for item in quote_facts)
    fresh_pct = round(valid_count / len(quote_facts) * 100, 2) if quote_facts else 0.0
    coverage_pct = round(len(quote_facts) / expected_count * 100, 2) if expected_count else 0.0
    sector_count = sum(item.symbol in SECTOR_SYMBOLS for item in quote_facts)
    sector_pct = round(sector_count / len(SECTOR_SYMBOLS) * 100, 2)
    news_validated = sum(item.validation_status == "valid" for item in news_evidence)
    news_dated = sum(bool(item.published_at) for item in news_evidence)
    limitations = list(data.warnings)
    if sector_count != len(SECTOR_SYMBOLS):
        limitations.append(f"行业 ETF 覆盖不完整：{sector_count}/{len(SECTOR_SYMBOLS)}")
    overall = "good"
    if coverage_pct < 90 or fresh_pct < 80 or sector_pct < 100 or not news_evidence:
        overall = "usable"
    if not news_evidence:
        limitations.append("没有可验证的新闻证据，热点、经济日历和财报驱动不可确认")
    if coverage_pct < 70 or fresh_pct < 50:
        overall = "limited"
    if coverage_pct < 40:
        overall = "poor"

    snapshot_id = generated_utc.strftime("usmi-%Y%m%dT%H%M%SZ")
    return USMarketIntelligenceSnapshot(
        snapshot_id=snapshot_id,
        generated_at=generated_utc.isoformat(),
        generated_at_new_york=data.generated_at.astimezone(NEW_YORK).isoformat(),
        market_session=market_session(data.generated_at),
        session_label=data.session_label,
        quotes=quote_facts,
        news=news_evidence,
        intelligence=AIIntelligence(status="fallback"),
        data_quality=DataQualitySummary(
            quote_coverage_pct=min(100.0, coverage_pct),
            fresh_quote_pct=fresh_pct,
            sector_etf_coverage_pct=sector_pct,
            news_total=len(news_evidence),
            news_validated=news_validated,
            news_with_published_at=news_dated,
            overall_status=overall,
            limitations=limitations,
        ),
        warnings=list(data.warnings),
    )


def build_machine_intelligence(snapshot: USMarketIntelligenceSnapshot) -> AIIntelligence:
    """Build machine-safe claims from deterministic facts; never infer news causality."""
    quote_map = {item.symbol: item for item in snapshot.quotes}
    claims: list[IntelligenceClaim] = []
    key_changes: list[str] = []
    triggers: list[MonitorTrigger] = []
    spy = quote_map.get("SPY")
    vix = quote_map.get("^VIX")
    if spy and spy.change_pct is not None:
        statement = f"SPY 当日变化 {spy.change_pct:+.2f}%"
        claims.append(IntelligenceClaim(statement=statement, kind="confirmed_fact", evidence_ids=[spy.evidence_id], confidence=0.98))
    if vix and vix.change_pct is not None:
        statement = f"VIX 当日变化 {vix.change_pct:+.2f}%"
        claims.append(IntelligenceClaim(statement=statement, kind="confirmed_fact", evidence_ids=[vix.evidence_id], confidence=0.98))

    sectors = sorted(
        (item for item in snapshot.quotes if item.symbol in SECTOR_SYMBOLS and item.change_pct is not None),
        key=lambda item: item.change_pct or 0.0,
        reverse=True,
    )
    sector_rotation = "数据不足"
    if sectors:
        leaders = ", ".join(item.symbol for item in sectors[:3])
        laggards = ", ".join(item.symbol for item in sectors[-3:])
        sector_rotation = f"领先：{leaders}；落后：{laggards}"
        claims.append(
            IntelligenceClaim(
                statement=f"行业 ETF 排名显示 {leaders} 领先，{laggards} 落后",
                kind="data_inference",
                evidence_ids=[item.evidence_id for item in [*sectors[:3], *sectors[-3:]]],
                confidence=0.9,
            )
        )

    risk_appetite = "数据不足"
    market_state = "数据不足"
    if spy and vix and spy.change_pct is not None and vix.change_pct is not None:
        if spy.change_pct > 0 and vix.change_pct < 0:
            risk_appetite = "风险偏好改善"
        elif spy.change_pct < 0 and vix.change_pct > 0:
            risk_appetite = "风险规避增强"
        else:
            risk_appetite = "风险信号分化"
        market_state = f"SPY {spy.change_pct:+.2f}%，VIX {vix.change_pct:+.2f}%"
        claims.append(
            IntelligenceClaim(
                statement=risk_appetite,
                kind="data_inference",
                evidence_ids=[spy.evidence_id, vix.evidence_id],
                confidence=0.85,
            )
        )

    for anomaly in snapshot.anomalies:
        key_changes.append(anomaly.reason)
        triggers.append(
            MonitorTrigger(
                event=anomaly.anomaly_type,
                condition=(
                    f"观察值 {anomaly.observed_value} {anomaly.unit or ''}，"
                    f"阈值 {anomaly.threshold} {anomaly.unit or ''}"
                    if anomaly.threshold is not None
                    else "继续监控该异常是否延续"
                ),
                rationale=anomaly.reason,
                evidence_ids=anomaly.evidence_ids,
            )
        )

    if snapshot.news:
        claims.append(
            IntelligenceClaim(
                statement="新闻仅作为潜在驱动线索，尚未确认与价格变化存在因果关系",
                kind="unverified_hypothesis",
                evidence_ids=[item.evidence_id for item in snapshot.news[:8]],
                confidence=0.3,
            )
        )
    return AIIntelligence(
        status="fallback",
        market_summary=market_state,
        market_state=market_state,
        risk_appetite=risk_appetite,
        sector_rotation=sector_rotation,
        key_changes=key_changes,
        claims=claims,
        monitor_triggers=triggers,
    )


def build_prompt(data: USMarketIntelligenceData, snapshot: Optional[USMarketIntelligenceSnapshot] = None) -> str:
    """Build a bounded, evidence-first Chinese synthesis prompt."""
    quote_lines = []
    for quote in data.quotes.values():
        horizons = ",".join(
            f"{label}={_fmt_pct(value)}" for label, value in quote.horizon_returns.items()
        )
        quote_lines.append(
            f"- {quote.symbol} {quote.name}: price={_fmt_number(quote.price)}, "
            f"change={_fmt_pct(quote.change_pct)}, horizons=[{horizons}], "
            f"as_of={quote.as_of or 'unknown'}, "
            f"stale_minutes={quote.stale_minutes if quote.stale_minutes is not None else 'unknown'}"
        )
    news_lines = [
        f"- [{item.category}] {item.title} | {item.source} | {item.published_date or '时间未知'} | "
        f"validation={item.validation_status} | {item.url} | snippet={item.snippet}"
        for item in data.news
        if item.validation_status == "valid"
    ][:12]
    comparison_lines: list[str] = []
    anomaly_lines: list[str] = []
    if snapshot is not None:
        for label, block in (
            ("上一小时", snapshot.comparison.previous_hour),
            ("上一交易日", snapshot.comparison.previous_trading_day),
        ):
            comparison_lines.append(f"- {label}: status={block.status}, baseline={block.baseline_snapshot_id}")
            comparison_lines.extend(
                f"  - {change.symbol}: price_change={change.price_change_pct}, "
                f"day_change_delta={change.day_change_delta_pct_points}, rank_change={change.sector_rank_change}"
                for change in block.changes
                if change.price_change_pct is not None or change.sector_rank_change is not None
            )
        anomaly_lines = [
            f"- {item.anomaly_type}/{item.scope}: {item.reason}; confidence={item.confidence}; "
            f"evidence={','.join(item.evidence_ids)}"
            for item in snapshot.anomalies
        ]
    return f"""你是一名严谨的美股跨资产市场策略师。请根据下方唯一可用的数据，生成中文 Markdown 情报正文。

规则：
1. 只能陈述输入中存在的事实，禁止编造经济数据、财报数字、发布日期或新闻内容。
2. 每个判断必须显式使用标签“【已确认事实】”“【数据推断】”或“【未经验证假设】”。
3. 新闻仅是未经验证线索。除非输入提供官方原始证据，否则禁止写成价格变化的原因；只能表述为“潜在驱动，尚未确认因果”。
4. 解释指数、利率、美元、黄金、原油和行业轮动之间的关系；没有证据时写“数据不足”。
5. 行业轮动以 11 个行业 ETF 相对 SPY 的当日表现、上一小时变化和排名变化为依据。
6. 数值异常仅采用下方规则引擎已触发的项目，禁止自行捏造阈值或异常。
7. 给出未来 1-3 小时观察清单和可验证触发条件，不给确定性收益承诺。
8. 使用固定章节并按此顺序：经济周期定位、金融故事主线与主要矛盾、多周期行业轮动、跨资产验证、异动与潜力股票、逐条新闻评价、监控事件与触发条件。
9. 多周期轮动必须分别解释 1日、5日、1个月、3个月、6个月；未来只给条件情景，不得把历史收益外推成确定预测。
10. 每条合格新闻都要给：核心结论、可能影响、进入哪条主线、证据等级、还需什么验证。无效或未校验新闻不进入正文。
11. 新闻引用使用 Markdown 链接；不要重复原始数据表，也不要添加免责声明页脚。

生成时间（纽约）：{data.generated_at.astimezone(NEW_YORK).isoformat()}
市场阶段：{data.session_label}

行情数据：
{chr(10).join(quote_lines) or '- 无可用行情'}

新闻数据：
{chr(10).join(news_lines) or '- 无可用新闻'}

历史比较：
{chr(10).join(comparison_lines) or '- 无历史比较基线'}

规则异常：
{chr(10).join(anomaly_lines) or '- 未触发规则异常'}

数据告警：
{chr(10).join(f'- {item}' for item in data.warnings) or '- 无'}
"""


def _fmt_number(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:,.2f}"


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2f}%"


def _market_regime_summary(data: USMarketIntelligenceData) -> list[str]:
    spy = data.quotes.get("SPY")
    qqq = data.quotes.get("QQQ")
    iwm = data.quotes.get("IWM")
    vix = data.quotes.get("^VIX")
    tnx = data.quotes.get("^TNX")
    dollar = data.quotes.get("DX-Y.NYB")
    lines: list[str] = []
    if spy and vix and spy.change_pct is not None and vix.change_pct is not None:
        if spy.change_pct < 0 and vix.change_pct > 5:
            lines.append("- **周期状态：风险收缩/去杠杆阶段。** 股指下跌且 VIX 明显上升，短线以防守和流动性管理优先。")
        elif spy.change_pct > 0 and vix.change_pct < 0:
            lines.append("- **周期状态：风险扩张阶段。** 股指上涨且波动率回落，风险资产内部轮动更值得关注。")
        else:
            lines.append("- **周期状态：分化/过渡阶段。** 股指与波动率信号未形成一致方向，需要等待确认。")
    else:
        lines.append("- **周期状态：数据不足。** 缺少 SPY 或 VIX 的可比数据。")
    if qqq and iwm and qqq.change_pct is not None and iwm.change_pct is not None:
        style = "大盘成长相对占优" if qqq.change_pct > iwm.change_pct else "小盘相对占优"
        lines.append(f"- **风格主线：{style}。** QQQ 与 IWM 当日相对差为 {qqq.change_pct - iwm.change_pct:+.2f} 个百分点。")
    if tnx and dollar and tnx.change_pct is not None and dollar.change_pct is not None:
        lines.append(
            f"- **主要矛盾：增长/估值与金融条件的博弈。** 10年期收益率 {tnx.change_pct:+.2f}%，"
            f"美元 {dollar.change_pct:+.2f}%；需判断风险变化来自增长预期还是流动性收紧。"
        )
    return lines


def _sector_rotation_table(data: USMarketIntelligenceData) -> str:
    spy = data.quotes.get("SPY")
    horizons = ("1d", "5d", "1m", "3m", "6m")
    headers = ["行业", *horizons, "当前判断"]
    lines = ["| " + " | ".join(headers) + " |", "|---|---:|---:|---:|---:|---:|---|"]
    for quote in data.sectors:
        relatives: list[Optional[float]] = []
        for horizon in horizons:
            sector_return = quote.horizon_returns.get(horizon)
            spy_return = spy.horizon_returns.get(horizon) if spy else None
            relatives.append(
                round(sector_return - spy_return, 2)
                if sector_return is not None and spy_return is not None
                else None
            )
        available = [value for value in relatives if value is not None]
        positive = sum(value > 0 for value in available)
        if len(available) >= 3 and positive >= max(2, len(available) - 1):
            regime = "多周期领先"
        elif len(available) >= 3 and positive <= 1:
            regime = "多周期落后"
        elif available and available[0] > 0:
            regime = "短线改善/待确认"
        else:
            regime = "轮动分化"
        values = [_fmt_pct(value) for value in relatives]
        lines.append(f"| {quote.name} ({quote.symbol}) | " + " | ".join([*values, regime]) + " |")
    return "\n".join(lines)


def _sector_horizon_outlook(data: USMarketIntelligenceData) -> str:
    spy = data.quotes.get("SPY")
    if spy is None:
        return "- 缺少 SPY 基准，无法形成分周期轮动情景。"
    horizon_specs = (
        ("未来1个交易日", "1d", "5d"),
        ("未来1个月", "1m", "5d"),
        ("未来3个月", "3m", "1m"),
        ("未来6个月", "6m", "3m"),
    )
    lines = []
    for label, primary, confirm in horizon_specs:
        ranked = []
        for quote in data.sectors:
            primary_value = quote.horizon_returns.get(primary)
            confirm_value = quote.horizon_returns.get(confirm)
            spy_primary = spy.horizon_returns.get(primary)
            spy_confirm = spy.horizon_returns.get(confirm)
            if primary_value is None or spy_primary is None:
                continue
            primary_relative = primary_value - spy_primary
            confirm_relative = (
                confirm_value - spy_confirm
                if confirm_value is not None and spy_confirm is not None
                else None
            )
            ranked.append((primary_relative, confirm_relative, quote.symbol))
        ranked.sort(reverse=True)
        if not ranked:
            lines.append(f"- **{label}：数据不足。** 尚无足够历史窗口。")
            continue
        leaders = ", ".join(item[2] for item in ranked[:3])
        confirmations = [item for item in ranked[:3] if item[1] is not None and item[1] > 0]
        if len(confirmations) >= 2:
            view = f"当前条件下 {leaders} 具备相对延续基础"
        else:
            view = f"{leaders} 仅是当前窗口领先，持续性尚未得到相邻周期确认"
        lines.append(
            f"- **{label}：{view}。** 若领先组合的相对收益转负或排名跌出前五，则该情景失效。"
        )
    return "\n".join(lines)


def _watchlist_opportunity_table(data: USMarketIntelligenceData) -> str:
    spy = data.quotes.get("SPY")
    rows = []
    for quote in data.watchlist:
        relative_1d = None
        relative_1m = None
        if spy:
            if quote.change_pct is not None and spy.change_pct is not None:
                relative_1d = quote.change_pct - spy.change_pct
            stock_1m = quote.horizon_returns.get("1m")
            spy_1m = spy.horizon_returns.get("1m")
            if stock_1m is not None and spy_1m is not None:
                relative_1m = stock_1m - spy_1m
        score = sum(value or 0 for value in (relative_1d, relative_1m))
        rows.append((score, quote, relative_1d, relative_1m))
    rows.sort(key=lambda item: item[0], reverse=True)
    lines = ["| 标的 | 当日相对SPY | 1月相对SPY | 观察结论 |", "|---|---:|---:|---|"]
    for _score, quote, relative_1d, relative_1m in rows[:8]:
        if relative_1d is not None and relative_1m is not None and relative_1d > 0 and relative_1m > 0:
            conclusion = "短中期相对强势，观察回撤承接"
        elif relative_1d is not None and relative_1d > 0:
            conclusion = "当日抗跌/走强，等待中期确认"
        else:
            conclusion = "弱于基准，暂不作为优先方向"
        lines.append(
            f"| {quote.symbol} | {_fmt_pct(relative_1d)} | {_fmt_pct(relative_1m)} | {conclusion} |"
        )
    return "\n".join(lines) if rows else "暂无观察名单数据。"


def _news_assessment(item: NewsItem) -> str:
    text = f"{item.title} {item.snippet}".lower()
    if any(term in text for term in ("fed", "yield", "inflation", "cpi", "jobs", "payroll")):
        theme = "货币政策与金融条件"
        impact = "可能影响利率路径、估值折现率与成长/价值风格"
    elif any(term in text for term in ("earnings", "revenue", "eps", "guidance")):
        theme = "企业盈利与预期修正"
        impact = "可能影响相关公司及行业盈利预期"
    elif any(term in text for term in ("oil", "energy", "opec")):
        theme = "能源与通胀链"
        impact = "可能影响能源板块、通胀预期和消费成本"
    else:
        theme = "市场风险偏好"
        impact = "可作为情绪或主线线索，但尚未确认价格因果"
    validation = "链接与发布时间已校验" if item.validation_status == "valid" else "证据未完全校验"
    return (
        f"- **{item.title}**（{item.source}）\n"
        f"  - 结论：归入“{theme}”主线；{impact}。\n"
        f"  - 证据等级：未经验证假设；{validation}，需以原始公告或权威来源复核。\n"
        f"  - 链接：{item.url}"
    )


def _quote_table(quotes: Iterable[QuotePoint], *, relative_to: Optional[float] = None) -> str:
    lines = ["| 标的 | 最新 | 日变动 | 相对SPY |", "|---|---:|---:|---:|"]
    count = 0
    for quote in quotes:
        relative = None
        if relative_to is not None and quote.change_pct is not None:
            relative = quote.change_pct - relative_to
        lines.append(
            f"| {quote.name} ({quote.symbol}) | {_fmt_number(quote.price)} | "
            f"{_fmt_pct(quote.change_pct)} | {_fmt_pct(relative) if relative_to is not None else '—'} |"
        )
        count += 1
    return "\n".join(lines) if count else "暂无可用数据。"


def deterministic_report(data: USMarketIntelligenceData) -> str:
    """Produce strategy-oriented analysis even when optional LLM synthesis fails."""
    macro = [data.quotes[symbol] for symbol in MACRO_SYMBOLS if symbol in data.quotes]
    valid_news = [item for item in data.news if item.validation_status == "valid"]
    sections = [
        "## 一、经济周期与市场状态",
        "\n".join(_market_regime_summary(data)),
        "## 二、金融故事主线与主要矛盾",
        "- 当前主线由风险偏好、利率/美元金融条件、盈利预期和行业相对强弱共同决定。",
        "- 主要矛盾应通过股指与 VIX、收益率与成长股、美元与商品、周期与防守板块是否互相验证来判断。",
        "- 下述新闻仅作为潜在驱动线索，不直接认定为价格变化原因。",
        "## 三、多周期行业轮动（相对 SPY）",
        _sector_rotation_table(data),
        "- 解释：正值代表该周期跑赢 SPY；负值代表跑输。短期与中期同向才视为较稳定轮动。",
        "- 未来情景：多周期领先若继续扩大，主线延续概率提高；若1日/5日率先转负，视为轮动衰减信号。",
        "### 分周期条件展望",
        _sector_horizon_outlook(data),
        "## 四、跨资产验证",
        _quote_table(macro),
        "## 五、值得关注的相对强势股票",
        _watchlist_opportunity_table(data),
        "## 六、新闻评价与主线映射",
        "\n".join(_news_assessment(item) for item in valid_news[:10])
        if valid_news
        else "暂无通过链接与发布时间校验的新闻，不对驱动因素作确认。",
        "## 七、监控事件与触发条件",
        "- 风险恶化：SPY/QQQ 继续下跌且 VIX 维持高位或进一步上行。",
        "- 风格切换：行业 ETF 的 1日和5日相对强弱同步反转，并延续到1个月窗口。",
        "- 金融条件收紧：10年期收益率与美元同步上行，同时成长板块继续跑输。",
        "- 主线确认：新闻线索获得官方公告/经济数据原文验证，并与价格、成交和行业扩散相互印证。",
    ]
    return "\n\n".join(sections)


def compose_report(
    data: USMarketIntelligenceData,
    analysis_body: str,
    snapshot: Optional[USMarketIntelligenceSnapshot] = None,
) -> str:
    market = [data.quotes[symbol] for symbol in MARKET_SYMBOLS if symbol in data.quotes]
    spy_change = data.quotes.get("SPY").change_pct if data.quotes.get("SPY") else None
    newest = max((quote.as_of for quote in data.quotes.values() if quote.as_of), default="未知")
    warning_text = "\n".join(f"- {warning}" for warning in data.warnings) or "- 无"
    snapshot_id = snapshot.snapshot_id if snapshot else "未生成"
    change_lines = ["- 无历史比较基线"]
    anomaly_lines = ["- 未触发规则异常"]
    quality_line = "未计算"
    if snapshot is not None:
        change_lines = []
        for label, block in (
            ("上一小时", snapshot.comparison.previous_hour),
            ("上一交易日", snapshot.comparison.previous_trading_day),
        ):
            if block.status != "available":
                change_lines.append(f"- {label}：无可用基线")
                continue
            material = [
                item
                for item in block.changes
                if item.price_change_pct is not None and abs(item.price_change_pct) >= 0.25
            ]
            change_lines.append(f"- {label}：{len(material)} 个标的变化超过 0.25%")
            change_lines.extend(
                f"  - {item.symbol}: {item.price_change_pct:+.2f}%"
                for item in sorted(material, key=lambda value: abs(value.price_change_pct or 0), reverse=True)[:6]
            )
            change_lines.extend(f"  - {message}" for message in block.leadership_changes)
        anomaly_lines = [
            f"- [{item.severity}] {item.reason}（置信度 {item.confidence:.0%}）"
            for item in snapshot.anomalies
        ] or ["- 未触发规则异常"]
        quality_line = (
            f"{snapshot.data_quality.overall_status}；行情覆盖 {snapshot.data_quality.quote_coverage_pct:.1f}%，"
            f"新鲜行情 {snapshot.data_quality.fresh_quote_pct:.1f}%，"
            f"行业 ETF {snapshot.data_quality.sector_etf_coverage_pct:.1f}%"
        )
    return f"""# 🇺🇸 美股每小时情报 · {data.session_label}

快照 ID：{snapshot_id}

纽约时间：{data.generated_at.astimezone(NEW_YORK):%Y-%m-%d %H:%M}
行情最新时间：{newest}

{analysis_body.strip()}

## 数据附录：核心市场

{_quote_table(market)}

## 数据附录：当日行业强弱

{_quote_table(data.sectors, relative_to=spy_change)}

## 数据附录：快照变化与规则异常

{chr(10).join(change_lines)}

{chr(10).join(anomaly_lines)}

## 数据质量

{quality_line}

## 数据边界

- 行情来自 Yahoo Finance 最新可用数据，可能延迟；不是交易所直连报价。
- 新闻来自 Brave 网页搜索，标题和链接用于线索发现，不等于已核验原始公告。
- 经济数据与企业财报以官方发布、SEC 文件和公司投资者关系页面为最终依据。
{warning_text}

以上仅供研究，不构成投资建议。
""".strip()


def run_us_market_intelligence(
    config: Any,
    *,
    watchlist: Optional[Iterable[str]] = None,
    now: Optional[datetime] = None,
    download: Optional[Callable[..., pd.DataFrame]] = None,
    generate: Optional[Callable[[str], str]] = None,
    validate_news_links: bool = True,
) -> USMarketIntelligenceData:
    """Collect data and attach a generated body for callers that want the full pipeline."""
    current = (now or datetime.now(tz=NEW_YORK)).astimezone(NEW_YORK)
    configured = watchlist if watchlist is not None else getattr(config, "stock_list", [])
    symbols = normalize_watchlist(configured)
    quotes, quote_warnings = fetch_us_market_snapshot(symbols, now=current, download=download)
    news, news_warnings = collect_us_news(config, now=current, validate_links=validate_news_links)
    data = USMarketIntelligenceData(
        generated_at=current,
        session_label=session_label(current),
        quotes=quotes,
        news=news,
        warnings=[*quote_warnings, *news_warnings],
    )
    if generate is not None:
        generate(build_prompt(data))
    return data


def configured_watchlist_from_env() -> list[str]:
    return normalize_watchlist(os.getenv("US_INTELLIGENCE_WATCHLIST") or os.getenv("STOCK_LIST") or "")
