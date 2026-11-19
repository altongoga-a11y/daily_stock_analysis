# -*- coding: utf-8 -*-
"""Hourly US market intelligence assembled from market snapshots and fresh news."""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

import pandas as pd

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


@dataclass(frozen=True)
class NewsItem:
    category: str
    title: str
    url: str
    source: str
    published_date: Optional[str] = None
    snippet: str = ""


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


def session_label(now: Optional[datetime] = None) -> str:
    current = (now or datetime.now(tz=NEW_YORK)).astimezone(NEW_YORK)
    minute = current.hour * 60 + current.minute
    if minute < 9 * 60 + 30:
        return "盘前"
    if minute < 16 * 60:
        return "盘中"
    return "盘后"


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
            period="10d",
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
    for symbol in symbols:
        price, timestamp = _latest_close(_ticker_frame(intraday, symbol, len(symbols)))
        previous_close = _previous_regular_close(_ticker_frame(daily, symbol, len(symbols)), current)
        change_pct = None
        if price is not None and previous_close not in (None, 0):
            change_pct = round((price - previous_close) / previous_close * 100, 2)

        as_of = None
        stale_minutes = None
        if timestamp is not None:
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize(UTC)
            timestamp_utc = timestamp.tz_convert(UTC)
            as_of = timestamp_utc.isoformat()
            stale_minutes = max(0, int((now_utc - timestamp_utc.to_pydatetime()).total_seconds() // 60))

        if price is not None:
            quotes[symbol] = QuotePoint(
                symbol=symbol,
                name=names[symbol],
                price=round(price, 4),
                change_pct=change_pct,
                as_of=as_of,
                stale_minutes=stale_minutes,
            )

    missing = [symbol for symbol in symbols if symbol not in quotes]
    if missing:
        warnings.append(f"缺失行情：{', '.join(missing[:12])}")
    return quotes, warnings


def collect_us_news(config: Any) -> tuple[list[NewsItem], list[str]]:
    """Collect fresh US macro, sector, calendar and earnings headlines through Brave."""
    warnings: list[str] = []
    keys = list(getattr(config, "brave_api_keys", []) or [])
    if not keys:
        return [], ["未配置 BRAVE_API_KEYS，新闻、经济日历和财报热点为空"]

    from src.search_service import BraveSearchProvider

    provider = BraveSearchProvider(keys)
    items: list[NewsItem] = []
    seen_urls: set[str] = set()
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
        for result in response.results:
            if not result.url or result.url in seen_urls:
                continue
            seen_urls.add(result.url)
            items.append(
                NewsItem(
                    category=category,
                    title=result.title.strip(),
                    url=result.url.strip(),
                    source=result.source.strip(),
                    published_date=result.published_date,
                    snippet=result.snippet.strip()[:400],
                )
            )
    return items, warnings


def build_prompt(data: USMarketIntelligenceData) -> str:
    """Build a bounded, evidence-first Chinese synthesis prompt."""
    quote_lines = []
    for quote in data.quotes.values():
        quote_lines.append(
            f"- {quote.symbol} {quote.name}: price={_fmt_number(quote.price)}, "
            f"change={_fmt_pct(quote.change_pct)}, as_of={quote.as_of or 'unknown'}, "
            f"stale_minutes={quote.stale_minutes if quote.stale_minutes is not None else 'unknown'}"
        )
    news_lines = [
        f"- [{item.category}] {item.title} | {item.source} | {item.published_date or '时间未知'} | {item.url}"
        for item in data.news[:20]
    ]
    return f"""你是一名严谨的美股跨资产市场策略师。请根据下方唯一可用的数据，生成中文 Markdown 情报正文。

规则：
1. 只能陈述输入中存在的事实；事实、推断、未知必须明确区分，禁止编造经济数据、财报数字、发布日期或新闻内容。
2. 解释指数、利率、美元、黄金、原油和行业轮动之间的关系；没有证据时写“数据不足”。
3. 行业轮动以 11 个行业 ETF 相对 SPY 的当日涨跌表现为依据，指出领先、落后和风格倾向。
4. 财报和经济日历只引用新闻标题能支持的内容，不得把搜索摘要当成已核验财务报表。
5. 给出未来 1-3 小时观察清单和风险触发条件，不给确定性收益承诺。
6. 使用以下固定章节：市场状态、跨资产主线、板块轮动、热点与新闻、经济与财报日历、观察名单、未来1-3小时检查项。
7. 新闻引用使用 Markdown 链接；不要重复输出原始数据表，也不要添加免责声明页脚。

生成时间（纽约）：{data.generated_at.astimezone(NEW_YORK).isoformat()}
市场阶段：{data.session_label}

行情数据：
{chr(10).join(quote_lines) or '- 无可用行情'}

新闻数据：
{chr(10).join(news_lines) or '- 无可用新闻'}

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
    """Produce a useful non-duplicating body when the LLM is unavailable."""
    macro = [data.quotes[symbol] for symbol in MACRO_SYMBOLS if symbol in data.quotes]
    news_lines = [
        f"- [{item.category}] [{item.title}]({item.url})（{item.source}）" for item in data.news[:12]
    ]
    return "\n\n".join(
        [
            "## 跨资产主线",
            _quote_table(macro),
            "## 大盘股异动",
            _quote_table(data.watchlist[:8]),
            "## 热点、经济与财报线索",
            "\n".join(news_lines) if news_lines else "暂无可用新闻；不对热点和催化作推断。",
            "## 未来1-3小时检查项",
            "LLM 暂不可用。本次仅展示可核验行情与新闻标题；请复核原始链接，并关注指数与领先板块是否延续。",
        ]
    )


def compose_report(data: USMarketIntelligenceData, analysis_body: str) -> str:
    market = [data.quotes[symbol] for symbol in MARKET_SYMBOLS if symbol in data.quotes]
    spy_change = data.quotes.get("SPY").change_pct if data.quotes.get("SPY") else None
    newest = max((quote.as_of for quote in data.quotes.values() if quote.as_of), default="未知")
    warning_text = "\n".join(f"- {warning}" for warning in data.warnings) or "- 无"
    return f"""# 🇺🇸 美股每小时情报 · {data.session_label}

纽约时间：{data.generated_at.astimezone(NEW_YORK):%Y-%m-%d %H:%M}
行情最新时间：{newest}

## 核心市场

{_quote_table(market)}

## 行业轮动速览

{_quote_table(data.sectors, relative_to=spy_change)}

{analysis_body.strip()}

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
) -> USMarketIntelligenceData:
    """Collect data and attach a generated body for callers that want the full pipeline."""
    current = (now or datetime.now(tz=NEW_YORK)).astimezone(NEW_YORK)
    configured = watchlist if watchlist is not None else getattr(config, "stock_list", [])
    symbols = normalize_watchlist(configured)
    quotes, quote_warnings = fetch_us_market_snapshot(symbols, now=current, download=download)
    news, news_warnings = collect_us_news(config)
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
