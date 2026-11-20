# -*- coding: utf-8 -*-
"""Orchestration layer that upgrades the existing US collector into an intelligence engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import pandas as pd

from src.schemas.us_market_intelligence import USMarketIntelligenceSnapshot
from src.services.us_market_intelligence import (
    SECTOR_SYMBOLS,
    USMarketIntelligenceData,
    build_machine_intelligence,
    build_prompt,
    build_structured_snapshot,
    compose_report,
    deterministic_report,
    run_us_market_intelligence,
)
from src.services.us_market_intelligence_anomalies import detect_market_anomalies
from src.services.us_market_intelligence_comparison import compare_with_baselines
from src.services.us_market_intelligence_history import USMarketIntelligenceHistory


@dataclass
class USMarketIntelligenceRunResult:
    data: USMarketIntelligenceData
    snapshot: USMarketIntelligenceSnapshot
    report: str
    history_path: Optional[Path] = None


def run_intelligence_engine(
    config: Any,
    *,
    watchlist: Iterable[str],
    now=None,
    download: Optional[Callable[..., pd.DataFrame]] = None,
    generate: Optional[Callable[[str], tuple[str, str, str]]] = None,
    history: Optional[USMarketIntelligenceHistory] = None,
    validate_news_links: bool = True,
) -> USMarketIntelligenceRunResult:
    data = run_us_market_intelligence(
        config,
        watchlist=watchlist,
        now=now,
        download=download,
        validate_news_links=validate_news_links,
    )
    snapshot = build_structured_snapshot(data)

    previous_hour = None
    previous_day = None
    if history is not None:
        previous_hour, previous_day = history.baselines(data.generated_at)
    snapshot.comparison = compare_with_baselines(
        snapshot,
        previous_hour=previous_hour,
        previous_trading_day=previous_day,
        sector_symbols=SECTOR_SYMBOLS,
    )
    snapshot.anomalies = detect_market_anomalies(snapshot, sector_symbols=SECTOR_SYMBOLS)
    snapshot.intelligence = build_machine_intelligence(snapshot)

    analysis_body = ""
    provider = None
    model = None
    if generate is not None:
        try:
            analysis_body, provider, model = generate(build_prompt(data, snapshot))
        except Exception as exc:
            data.warnings.append(f"LLM 生成失败：{type(exc).__name__}")
    if analysis_body:
        snapshot.intelligence.status = "generated"
        snapshot.intelligence.narrative_markdown = analysis_body
        snapshot.intelligence.provider = provider or None
        snapshot.intelligence.model = model or None
    else:
        analysis_body = deterministic_report(data)
        snapshot.intelligence.status = "fallback"
        snapshot.intelligence.narrative_markdown = analysis_body
        snapshot.intelligence.warnings.append("未生成 AI 叙事，不对新闻和价格变化建立因果关系")

    report = compose_report(data, analysis_body, snapshot)
    snapshot.report_markdown = report
    snapshot.warnings = list(dict.fromkeys([*snapshot.warnings, *data.warnings]))
    history_path = history.save(snapshot) if history is not None else None
    return USMarketIntelligenceRunResult(
        data=data,
        snapshot=snapshot,
        report=report,
        history_path=history_path,
    )
