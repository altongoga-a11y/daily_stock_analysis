# -*- coding: utf-8 -*-
"""Deterministic anomaly detection for US market intelligence snapshots."""

from __future__ import annotations

import hashlib
from typing import Iterable

from src.schemas.us_market_intelligence import MarketAnomaly, USMarketIntelligenceSnapshot


def _anomaly_id(kind: str, scope: str, as_of: str) -> str:
    digest = hashlib.sha256(f"{kind}|{scope}|{as_of}".encode("utf-8")).hexdigest()[:16]
    return f"anomaly-{digest}"


def _make_anomaly(
    snapshot: USMarketIntelligenceSnapshot,
    *,
    anomaly_type: str,
    scope: str,
    severity: str,
    reason: str,
    evidence_ids: Iterable[str],
    confidence: float,
    observed_value: float | None = None,
    threshold: float | None = None,
    unit: str | None = None,
    diagnostics: dict | None = None,
) -> MarketAnomaly:
    return MarketAnomaly(
        anomaly_id=_anomaly_id(anomaly_type, scope, snapshot.generated_at),
        anomaly_type=anomaly_type,
        scope=scope,
        severity=severity,
        status="triggered",
        observed_value=observed_value,
        threshold=threshold,
        unit=unit,
        reason=reason,
        evidence_ids=list(evidence_ids),
        confidence=confidence,
        as_of=snapshot.generated_at,
        diagnostics=diagnostics or {},
    )


def detect_market_anomalies(
    snapshot: USMarketIntelligenceSnapshot,
    *,
    sector_symbols: Iterable[str],
    rapid_move_threshold_pct: float = 0.75,
    vix_jump_threshold_pct: float = 3.0,
    sector_relative_threshold_pct: float = 1.5,
) -> list[MarketAnomaly]:
    anomalies: list[MarketAnomaly] = []
    quote_map = {quote.symbol: quote for quote in snapshot.quotes}

    stale_or_bad = [
        quote for quote in snapshot.quotes if quote.provenance.validation_status not in {"valid"}
    ]
    if stale_or_bad:
        anomalies.append(
            _make_anomaly(
                snapshot,
                anomaly_type="stale_or_missing_data",
                scope="market_snapshot",
                severity="warning",
                reason=f"{len(stale_or_bad)} 条行情未通过有效性校验",
                evidence_ids=[quote.evidence_id for quote in stale_or_bad],
                confidence=1.0,
                observed_value=float(len(stale_or_bad)),
                threshold=0.0,
                unit="records",
            )
        )

    previous_hour = snapshot.comparison.previous_hour
    if previous_hour.status == "available":
        for change in previous_hour.changes:
            if change.price_change_pct is None or abs(change.price_change_pct) < rapid_move_threshold_pct:
                continue
            quote = quote_map.get(change.symbol)
            if quote is None:
                continue
            anomalies.append(
                _make_anomaly(
                    snapshot,
                    anomaly_type="rapid_price_move",
                    scope=change.symbol,
                    severity="critical" if abs(change.price_change_pct) >= rapid_move_threshold_pct * 2 else "warning",
                    reason=f"{change.symbol} 相对上一快照变动 {change.price_change_pct:+.2f}%",
                    evidence_ids=[quote.evidence_id],
                    confidence=0.95 if quote.provenance.validation_status == "valid" else 0.55,
                    observed_value=change.price_change_pct,
                    threshold=rapid_move_threshold_pct,
                    unit="percent",
                )
            )

        vix_change = next((item for item in previous_hour.changes if item.symbol == "^VIX"), None)
        if vix_change and vix_change.price_change_pct is not None and vix_change.price_change_pct >= vix_jump_threshold_pct:
            quote = quote_map.get("^VIX")
            anomalies.append(
                _make_anomaly(
                    snapshot,
                    anomaly_type="vix_jump",
                    scope="^VIX",
                    severity="critical" if vix_change.price_change_pct >= vix_jump_threshold_pct * 2 else "warning",
                    reason=f"VIX 相对上一快照上升 {vix_change.price_change_pct:+.2f}%",
                    evidence_ids=[quote.evidence_id] if quote else [],
                    confidence=0.95,
                    observed_value=vix_change.price_change_pct,
                    threshold=vix_jump_threshold_pct,
                    unit="percent",
                )
            )

        for message in previous_hour.leadership_changes:
            anomalies.append(
                _make_anomaly(
                    snapshot,
                    anomaly_type="sector_leadership_reversal",
                    scope="sectors",
                    severity="info",
                    reason=message,
                    evidence_ids=[
                        quote.evidence_id for quote in snapshot.quotes if quote.symbol in set(sector_symbols)
                    ],
                    confidence=0.9,
                )
            )

    spy = quote_map.get("SPY")
    if spy and spy.change_pct is not None:
        for symbol in sector_symbols:
            quote = quote_map.get(symbol)
            if quote is None or quote.change_pct is None:
                continue
            relative = quote.change_pct - spy.change_pct
            if abs(relative) >= sector_relative_threshold_pct:
                anomalies.append(
                    _make_anomaly(
                        snapshot,
                        anomaly_type="sector_relative_divergence",
                        scope=symbol,
                        severity="warning",
                        reason=f"{symbol} 相对 SPY 偏离 {relative:+.2f} 个百分点",
                        evidence_ids=[spy.evidence_id, quote.evidence_id],
                        confidence=0.9,
                        observed_value=relative,
                        threshold=sector_relative_threshold_pct,
                        unit="percentage_points",
                    )
                )

    invalid_news = [item for item in snapshot.news if item.validation_status not in {"valid"}]
    if snapshot.news and len(invalid_news) / len(snapshot.news) >= 0.5:
        anomalies.append(
            _make_anomaly(
                snapshot,
                anomaly_type="news_quality_degradation",
                scope="news",
                severity="warning",
                reason="超过一半新闻缺少有效链接、发布时间或可达性验证",
                evidence_ids=[item.evidence_id for item in invalid_news],
                confidence=1.0,
                observed_value=round(len(invalid_news) / len(snapshot.news) * 100, 2),
                threshold=50.0,
                unit="percent",
            )
        )

    # Cross-asset relationship is a deterministic warning, never a causal claim.
    tnx = quote_map.get("^TNX")
    qqq = quote_map.get("QQQ")
    if tnx and qqq and tnx.change_pct is not None and qqq.change_pct is not None:
        if tnx.change_pct >= 1.0 and qqq.change_pct >= 1.0:
            anomalies.append(
                _make_anomaly(
                    snapshot,
                    anomaly_type="cross_asset_divergence",
                    scope="QQQ,^TNX",
                    severity="info",
                    reason="纳指代理与10年期收益率同步走强；这是相关性背离观察，不代表因果",
                    evidence_ids=[tnx.evidence_id, qqq.evidence_id],
                    confidence=0.8,
                    diagnostics={"causal_claim": False},
                )
            )

    return anomalies
