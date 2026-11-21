# -*- coding: utf-8 -*-
"""Orchestration layer that upgrades the existing US collector into an intelligence engine."""

from __future__ import annotations

import os
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
from src.services.us_macro_intelligence import (
    assess_macro_cycle,
    fetch_bls_macro_facts,
    fetch_fxmacro_release_calendar,
)
from src.services.us_market_intelligence_anomalies import detect_market_anomalies
from src.services.us_market_intelligence_audit import audit_snapshot
from src.services.us_market_intelligence_comparison import compare_with_baselines
from src.services.us_market_intelligence_history import USMarketIntelligenceHistory
from src.services.us_news_intelligence import cluster_news
from src.services.us_sec_intelligence import collect_sec_evidence


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
    collect_authoritative_evidence: bool = True,
) -> USMarketIntelligenceRunResult:
    data = run_us_market_intelligence(
        config,
        watchlist=watchlist,
        now=now,
        download=download,
        validate_news_links=validate_news_links,
    )
    snapshot = build_structured_snapshot(data)
    if collect_authoritative_evidence:
        macro_facts, macro_warnings = fetch_bls_macro_facts(
            now=data.generated_at,
            registration_key=os.getenv("BLS_REGISTRATION_KEY") or getattr(config, "bls_registration_key", None),
        )
        next_releases, calendar_warnings = fetch_fxmacro_release_calendar(
            now=data.generated_at,
            api_key=os.getenv("FXMACRODATA_API_KEY"),
        )
        snapshot.macro_facts = macro_facts
        snapshot.macro_cycle = assess_macro_cycle(macro_facts, next_releases=next_releases)
        data.warnings.extend([*macro_warnings, *calendar_warnings])
        snapshot.company_evidence = collect_sec_evidence(
            list(watchlist),
            os.getenv("SEC_USER_AGENT") or getattr(config, "sec_user_agent", None),
        )
    snapshot.news_clusters = cluster_news(
        [item for item in snapshot.news if item.validation_status == "valid"]
    )

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
    generation_error = None
    if generate is not None:
        try:
            analysis_body, provider, model = generate(build_prompt(data, snapshot))
            if not analysis_body:
                generation_error = "模型返回空内容；请检查模型名称、权限、余额或 API 配额"
        except Exception as exc:
            generation_error = f"{type(exc).__name__}: {str(exc)[:240]}"
    if generation_error:
        data.warnings.append(f"LLM 生成失败：{generation_error}")
    strategy_body = deterministic_report(data, snapshot)
    if analysis_body:
        snapshot.intelligence.status = "generated"
        snapshot.intelligence.narrative_markdown = analysis_body
        snapshot.intelligence.provider = provider or None
        snapshot.intelligence.model = model or None
        report_body = f"{strategy_body}\n\n## 八、AI 深度研判\n\n{analysis_body}"
    else:
        snapshot.intelligence.status = "fallback"
        snapshot.intelligence.narrative_markdown = strategy_body
        snapshot.intelligence.warnings.append(
            f"未生成 AI 叙事：{generation_error or '未配置生成器'}；已使用确定性策略报告"
        )
        report_body = strategy_body

    report = compose_report(data, report_body, snapshot)
    snapshot.report_markdown = report
    snapshot.audit = audit_snapshot(snapshot, report)
    snapshot.warnings = list(dict.fromkeys([*snapshot.warnings, *data.warnings]))
    history_path = history.save(snapshot) if history is not None else None
    return USMarketIntelligenceRunResult(
        data=data,
        snapshot=snapshot,
        report=report,
        history_path=history_path,
    )
