# -*- coding: utf-8 -*-
"""Deterministic factual audit for US market intelligence outputs."""

from __future__ import annotations

import re

from src.schemas.us_market_intelligence import (
    ReportAuditFinding,
    ReportAuditResult,
    USMarketIntelligenceSnapshot,
)

_CAUSAL_PATTERNS = (
    re.compile(r"(?:because of|due to|caused by)", re.I),
    re.compile(r"(?:因为|由于).{0,80}(?:导致|引发|推动)"),
)


def audit_snapshot(snapshot: USMarketIntelligenceSnapshot, report_markdown: str) -> ReportAuditResult:
    findings: list[ReportAuditFinding] = []
    evidence_status = {quote.evidence_id: quote.provenance.validation_status for quote in snapshot.quotes}
    evidence_status.update({item.evidence_id: item.validation_status for item in snapshot.news})
    evidence_status.update({item.evidence_id: item.validation_status for item in snapshot.macro_facts})
    for company in snapshot.company_evidence:
        evidence_status.update({item.evidence_id: item.validation_status for item in company.facts})

    if snapshot.snapshot_id not in report_markdown:
        findings.append(ReportAuditFinding(code="snapshot_id_missing", severity="critical", message="Markdown 缺少当前快照 ID"))
    for claim in snapshot.intelligence.claims:
        if not claim.evidence_ids:
            findings.append(
                ReportAuditFinding(code="claim_without_evidence", severity="critical", message="结构化结论缺少证据", claim=claim.statement)
            )
            continue
        missing = [item for item in claim.evidence_ids if item not in evidence_status]
        if missing:
            findings.append(
                ReportAuditFinding(
                    code="unknown_evidence_id",
                    severity="critical",
                    message=f"结论引用未知证据：{', '.join(missing)}",
                    claim=claim.statement,
                    evidence_ids=missing,
                )
            )
        if claim.kind == "confirmed_fact":
            degraded = [item for item in claim.evidence_ids if evidence_status.get(item) != "valid"]
            if degraded:
                findings.append(
                    ReportAuditFinding(
                        code="confirmed_fact_uses_degraded_evidence",
                        severity="critical",
                        message="已确认事实引用了未通过校验的证据",
                        claim=claim.statement,
                        evidence_ids=degraded,
                    )
                )

    if snapshot.news and any(pattern.search(report_markdown) for pattern in _CAUSAL_PATTERNS):
        findings.append(
            ReportAuditFinding(
                code="unsupported_news_causality",
                severity="warning",
                message="报告包含新闻因果措辞；需人工确认是否有官方原始证据",
            )
        )
    critical = any(item.severity == "critical" for item in findings)
    return ReportAuditResult(passed=not critical, notification_allowed=not critical, findings=findings)
