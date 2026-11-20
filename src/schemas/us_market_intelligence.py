# -*- coding: utf-8 -*-
"""Versioned contracts for the US Market Intelligence Engine."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

US_MARKET_INTELLIGENCE_SCHEMA_VERSION = "1.0"

FactKind = Literal["confirmed_fact", "data_inference", "unverified_hypothesis"]
ValidationStatus = Literal["valid", "stale", "missing", "invalid", "unverified"]
MarketSession = Literal["premarket", "intraday", "postmarket", "non_trading", "unknown"]
Severity = Literal["info", "warning", "critical"]
AnomalyStatus = Literal["triggered", "not_triggered", "degraded", "skipped"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuoteProvenance(_StrictModel):
    provider: str
    source_type: Literal["market_data"] = "market_data"
    provider_timestamp: Optional[str] = None
    retrieved_at: str
    market_time: Optional[str] = None
    market_session: MarketSession
    ttl_seconds: int = Field(ge=0)
    validation_status: ValidationStatus
    validation_reasons: List[str] = Field(default_factory=list)


class QuoteFact(_StrictModel):
    evidence_id: str
    symbol: str
    name: str
    price: Optional[float] = None
    change_pct: Optional[float] = None
    previous_close: Optional[float] = None
    horizon_returns: Dict[str, Optional[float]] = Field(default_factory=dict)
    stale_seconds: Optional[int] = Field(default=None, ge=0)
    fact_kind: Literal["confirmed_fact"] = "confirmed_fact"
    provenance: QuoteProvenance


class NewsEvidence(_StrictModel):
    evidence_id: str
    category: str
    title: str
    url: str
    source: str
    published_at: Optional[str] = None
    retrieved_at: str
    snippet: str = ""
    fact_kind: Literal["unverified_hypothesis"] = "unverified_hypothesis"
    validation_status: ValidationStatus = "unverified"
    validation_reasons: List[str] = Field(default_factory=list)
    http_status: Optional[int] = None


class SymbolChange(_StrictModel):
    symbol: str
    current_price: Optional[float] = None
    previous_price: Optional[float] = None
    price_change_pct: Optional[float] = None
    current_day_change_pct: Optional[float] = None
    previous_day_change_pct: Optional[float] = None
    day_change_delta_pct_points: Optional[float] = None
    sector_rank_current: Optional[int] = None
    sector_rank_previous: Optional[int] = None
    sector_rank_change: Optional[int] = None


class ComparisonBlock(_StrictModel):
    baseline_snapshot_id: Optional[str] = None
    baseline_generated_at: Optional[str] = None
    status: Literal["available", "no_baseline", "invalid_baseline"] = "no_baseline"
    changes: List[SymbolChange] = Field(default_factory=list)
    new_news_urls: List[str] = Field(default_factory=list)
    leadership_changes: List[str] = Field(default_factory=list)


class SnapshotComparison(_StrictModel):
    previous_hour: ComparisonBlock = Field(default_factory=ComparisonBlock)
    previous_trading_day: ComparisonBlock = Field(default_factory=ComparisonBlock)


class MarketAnomaly(_StrictModel):
    anomaly_id: str
    anomaly_type: Literal[
        "rapid_price_move",
        "vix_jump",
        "sector_relative_divergence",
        "sector_leadership_reversal",
        "cross_asset_divergence",
        "stale_or_missing_data",
        "news_quality_degradation",
    ]
    scope: str
    severity: Severity
    status: AnomalyStatus
    observed_value: Optional[float] = None
    threshold: Optional[float] = None
    unit: Optional[str] = None
    reason: str
    evidence_ids: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    as_of: str
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class IntelligenceClaim(_StrictModel):
    statement: str
    kind: FactKind
    evidence_ids: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class MonitorTrigger(_StrictModel):
    event: str
    condition: str
    rationale: str
    evidence_ids: List[str] = Field(default_factory=list)


class AIIntelligence(_StrictModel):
    status: Literal["generated", "fallback"] = "fallback"
    narrative_markdown: Optional[str] = None
    market_summary: str = ""
    market_state: str = "数据不足"
    risk_appetite: str = "数据不足"
    sector_rotation: str = "数据不足"
    key_changes: List[str] = Field(default_factory=list)
    claims: List[IntelligenceClaim] = Field(default_factory=list)
    monitor_triggers: List[MonitorTrigger] = Field(default_factory=list)
    model: Optional[str] = None
    provider: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


class DataQualitySummary(_StrictModel):
    quote_coverage_pct: float = Field(ge=0, le=100)
    fresh_quote_pct: float = Field(ge=0, le=100)
    sector_etf_coverage_pct: float = Field(ge=0, le=100)
    news_total: int = Field(ge=0)
    news_validated: int = Field(ge=0)
    news_with_published_at: int = Field(ge=0)
    overall_status: Literal["good", "usable", "limited", "poor"]
    limitations: List[str] = Field(default_factory=list)


class USMarketIntelligenceSnapshot(_StrictModel):
    schema_version: Literal["1.0"] = US_MARKET_INTELLIGENCE_SCHEMA_VERSION
    snapshot_id: str
    generated_at: str
    generated_at_new_york: str
    market_session: MarketSession
    session_label: str
    quotes: List[QuoteFact]
    news: List[NewsEvidence]
    comparison: SnapshotComparison = Field(default_factory=SnapshotComparison)
    anomalies: List[MarketAnomaly] = Field(default_factory=list)
    intelligence: AIIntelligence = Field(default_factory=AIIntelligence)
    data_quality: DataQualitySummary
    warnings: List[str] = Field(default_factory=list)
    report_markdown: Optional[str] = None

    def to_json_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)
