# -*- coding: utf-8 -*-
"""Deterministic comparison of US market intelligence snapshots."""

from __future__ import annotations

from typing import Dict, Iterable, Optional

from src.schemas.us_market_intelligence import (
    ComparisonBlock,
    QuoteFact,
    SnapshotComparison,
    SymbolChange,
    USMarketIntelligenceSnapshot,
)


def _quote_map(quotes: Iterable[QuoteFact]) -> Dict[str, QuoteFact]:
    return {quote.symbol: quote for quote in quotes}


def _sector_ranks(snapshot: USMarketIntelligenceSnapshot, sector_symbols: Iterable[str]) -> Dict[str, int]:
    allowed = set(sector_symbols)
    ranked = sorted(
        (quote for quote in snapshot.quotes if quote.symbol in allowed and quote.change_pct is not None),
        key=lambda quote: quote.change_pct or 0.0,
        reverse=True,
    )
    return {quote.symbol: index + 1 for index, quote in enumerate(ranked)}


def _pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous in (None, 0):
        return None
    return round((current - previous) / previous * 100, 4)


def compare_snapshot(
    current: USMarketIntelligenceSnapshot,
    baseline: Optional[USMarketIntelligenceSnapshot],
    *,
    sector_symbols: Iterable[str],
) -> ComparisonBlock:
    if baseline is None:
        return ComparisonBlock(status="no_baseline")

    current_quotes = _quote_map(current.quotes)
    baseline_quotes = _quote_map(baseline.quotes)
    current_ranks = _sector_ranks(current, sector_symbols)
    baseline_ranks = _sector_ranks(baseline, sector_symbols)
    changes = []
    for symbol, quote in current_quotes.items():
        previous = baseline_quotes.get(symbol)
        if previous is None:
            continue
        current_rank = current_ranks.get(symbol)
        previous_rank = baseline_ranks.get(symbol)
        day_delta = None
        if quote.change_pct is not None and previous.change_pct is not None:
            day_delta = round(quote.change_pct - previous.change_pct, 4)
        changes.append(
            SymbolChange(
                symbol=symbol,
                current_price=quote.price,
                previous_price=previous.price,
                price_change_pct=_pct_change(quote.price, previous.price),
                current_day_change_pct=quote.change_pct,
                previous_day_change_pct=previous.change_pct,
                day_change_delta_pct_points=day_delta,
                sector_rank_current=current_rank,
                sector_rank_previous=previous_rank,
                sector_rank_change=(previous_rank - current_rank) if current_rank and previous_rank else None,
            )
        )

    prior_urls = {item.url for item in baseline.news}
    new_urls = [item.url for item in current.news if item.url not in prior_urls]
    leadership = []
    if current_ranks and baseline_ranks:
        current_leader = min(current_ranks, key=current_ranks.get)
        previous_leader = min(baseline_ranks, key=baseline_ranks.get)
        if current_leader != previous_leader:
            leadership.append(f"行业领先由 {previous_leader} 切换为 {current_leader}")

    return ComparisonBlock(
        baseline_snapshot_id=baseline.snapshot_id,
        baseline_generated_at=baseline.generated_at,
        status="available",
        changes=changes,
        new_news_urls=new_urls,
        leadership_changes=leadership,
    )


def compare_with_baselines(
    current: USMarketIntelligenceSnapshot,
    *,
    previous_hour: Optional[USMarketIntelligenceSnapshot],
    previous_trading_day: Optional[USMarketIntelligenceSnapshot],
    sector_symbols: Iterable[str],
) -> SnapshotComparison:
    return SnapshotComparison(
        previous_hour=compare_snapshot(current, previous_hour, sector_symbols=sector_symbols),
        previous_trading_day=compare_snapshot(current, previous_trading_day, sector_symbols=sector_symbols),
    )
