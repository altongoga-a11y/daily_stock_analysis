# -*- coding: utf-8 -*-
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.schemas.us_market_intelligence import (
    AIIntelligence,
    DataQualitySummary,
    MacroFact,
    IntelligenceClaim,
    MacroCycleAssessment,
    NewsEvidence,
    QuoteFact,
    QuoteProvenance,
    USMarketIntelligenceSnapshot,
)
from src.services.us_macro_intelligence import (
    assess_macro_cycle,
    fetch_bls_macro_facts,
    fetch_fxmacro_release_calendar,
)
from src.services.us_market_intelligence_audit import audit_snapshot
from src.services.us_market_intelligence_delivery import DeliveryLedger, decide_delivery
from src.services.us_news_intelligence import canonicalize_news_url, cluster_news, source_tier
from src.services.us_sec_intelligence import SecEdgarClient, collect_sec_evidence

UTC = ZoneInfo("UTC")


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _snapshot():
    quote = QuoteFact(
        evidence_id="quote-spy",
        symbol="SPY",
        name="SPY",
        price=100,
        change_pct=1,
        provenance=QuoteProvenance(
            provider="Yahoo Finance",
            retrieved_at="2026-11-20T15:00:00+00:00",
            market_session="intraday",
            ttl_seconds=900,
            validation_status="valid",
        ),
    )
    return USMarketIntelligenceSnapshot(
        snapshot_id="snapshot-1",
        generated_at="2026-11-20T15:00:00+00:00",
        generated_at_new_york="2026-11-20T10:00:00-05:00",
        market_session="intraday",
        session_label="盘中",
        quotes=[quote],
        news=[],
        data_quality=DataQualitySummary(
            quote_coverage_pct=100,
            fresh_quote_pct=100,
            sector_etf_coverage_pct=100,
            news_total=0,
            news_validated=0,
            news_with_published_at=0,
            overall_status="usable",
        ),
    )


def test_bls_public_batch_preserves_units_period_and_adjustment():
    payload = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": "LNS14000000",
                    "data": [
                        {"year": "2026", "period": "M10", "value": "4.2"},
                        {"year": "2026", "period": "M09", "value": "4.1"},
                        {"year": "2026", "period": "M08", "value": "4.0"},
                    ],
                }
            ]
        },
    }
    facts, warnings = fetch_bls_macro_facts(
        now=datetime(2026, 11, 20, tzinfo=UTC),
        post=lambda *args, **kwargs: _Response(payload),
    )
    assert warnings == []
    assert facts[0].provider == "U.S. Bureau of Labor Statistics"
    assert facts[0].unit == "percent"
    assert facts[0].period == "2026-10"
    assert facts[0].seasonal_adjustment == "seasonally_adjusted"


def test_fxmacro_calendar_keeps_confirmed_high_impact_next_week():
    class _Result:
        @staticmethod
        def as_dict():
            return {
                "data": {
                    "data": [
                        {
                            "announcement_datetime_utc": "2026-11-24T13:30:00+00:00",
                            "announcement_datetime_requested_timezone": "2026-11-24T08:30:00-05:00",
                            "name": "Core PCE",
                            "top_tier_for_currency": True,
                            "market_tier": 1,
                            "event_importance": "high",
                            "source_url": "https://bea.gov/release",
                        },
                        {
                            "announcement_datetime_utc": "2026-12-24T13:30:00+00:00",
                            "name": "Too far",
                            "top_tier_for_currency": True,
                            "market_tier": 1,
                        },
                    ]
                }
            }

    class _Client:
        def execute(self, operation, arguments):
            assert operation == "release_calendar"
            assert arguments["currency"] == "USD"
            return _Result()

        def close(self):
            pass

    releases, warnings = fetch_fxmacro_release_calendar(
        now=datetime(2026, 11, 20, tzinfo=UTC),
        client_factory=lambda **kwargs: _Client(),
    )
    assert warnings == []
    assert len(releases) == 1
    assert "Core PCE" in releases[0]
    assert "bea.gov" in releases[0]


def test_macro_cycle_classifies_goldilocks_from_growth_and_inflation_directions():
    def fact(series_id, values):
        return MacroFact(
            evidence_id=f"m-{series_id}",
            series_id=series_id,
            name=series_id,
            value=values[-1],
            unit="index",
            period="2026-10",
            frequency="monthly",
            seasonal_adjustment="seasonally_adjusted",
            provider="BLS",
            source_url="https://bls.gov",
            retrieved_at="2026-11-20T00:00:00+00:00",
            metadata={"observations": [{"period": str(i), "value": value} for i, value in enumerate(values)]},
        )

    core_cpi = [100 + i * 0.4 for i in range(12)] + [105.0, 105.2]
    unemployment = [4.3, 4.2, 4.1]
    payroll = [100, 110, 121, 135]
    facts = [
        fact("CUUR0000SA0", core_cpi),
        fact("CUUR0000SA0L1E", core_cpi),
        fact("LNS14000000", unemployment),
        fact("CES0000000001", payroll),
    ]
    assessment = assess_macro_cycle(facts)
    assert assessment.growth_direction == "rising"
    assert assessment.inflation_direction == "falling"
    assert assessment.quadrant == "goldilocks"
    assert assessment.status == "supported"


def test_macro_cycle_refuses_quadrant_when_required_evidence_missing():
    assessment = assess_macro_cycle([])
    assert assessment.status == "insufficient"
    assert assessment.quadrant == "undetermined"
    assert assessment.missing_series


def test_sec_requires_contact_user_agent_and_disables_without_it():
    with pytest.raises(ValueError):
        SecEdgarClient("daily-stock-analysis")
    disabled = collect_sec_evidence(["AAPL"], None)
    assert disabled[0].status == "disabled"
    assert "SEC_USER_AGENT" in disabled[0].warnings[0]


def test_sec_client_resolves_cik_and_preserves_accession_and_form(tmp_path):
    ticker_payload = {"0": {"ticker": "AAPL", "cik_str": 320193}}
    submissions = {"filings": {"recent": {"form": ["10-Q", "8-K"]}}}
    facts = {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {
                                "val": 1000,
                                "form": "10-Q",
                                "filed": "2026-10-30",
                                "end": "2026-09-30",
                                "accn": "0000320193-26-000001",
                            }
                        ]
                    }
                }
            }
        }
    }
    responses = iter([ticker_payload, submissions, facts])
    client = SecEdgarClient(
        "daily-stock-analysis contact@example.com",
        get=lambda *args, **kwargs: _Response(next(responses)),
        cache_dir=tmp_path,
    )
    evidence = client.company_evidence("AAPL")
    assert evidence.status == "available"
    assert evidence.facts[0].form == "10-Q"
    assert evidence.facts[0].accession == "0000320193-26-000001"
    assert evidence.facts[0].source_url.startswith("https://www.sec.gov/Archives/")


def test_news_clustering_assigns_tier_and_independent_corroboration():
    evidence = [
        NewsEvidence(
            evidence_id="n1",
            category="市场主线",
            title="Federal Reserve signals rate pause",
            url="https://reuters.com/a?utm_source=x",
            source="reuters.com",
            published_at="2026-11-20T14:00:00+00:00",
            retrieved_at="2026-11-20T15:00:00+00:00",
            validation_status="valid",
        ),
        NewsEvidence(
            evidence_id="n2",
            category="市场主线",
            title="Fed signals a rate pause",
            url="https://cnbc.com/b",
            source="cnbc.com",
            published_at="2026-11-20T14:05:00+00:00",
            retrieved_at="2026-11-20T15:00:00+00:00",
            validation_status="valid",
        ),
    ]
    clusters = cluster_news(evidence)
    assert len(clusters) == 1
    assert clusters[0].source_tier == "tier1"
    assert clusters[0].independent_source_count == 2
    assert clusters[0].corroboration_status == "corroborated"
    assert canonicalize_news_url(evidence[0].url) == "https://reuters.com/a"
    assert source_tier("sec.gov") == "official"


def test_audit_blocks_confirmed_claim_with_unknown_evidence():
    snapshot = _snapshot()
    snapshot.intelligence = AIIntelligence(
        claims=[
            IntelligenceClaim(
                statement="unsupported fact",
                kind="confirmed_fact",
                evidence_ids=["missing"],
                confidence=1,
            )
        ]
    )
    result = audit_snapshot(snapshot, "snapshot-1")
    assert result.passed is False
    assert result.notification_allowed is False
    assert any(item.code == "unknown_evidence_id" for item in result.findings)


def test_delivery_ledger_persists_only_after_record(tmp_path):
    snapshot = _snapshot()
    decision = decide_delivery(snapshot)
    ledger = DeliveryLedger(tmp_path / "delivery.json")
    assert ledger.load() == {}
    ledger.record(snapshot, decision)
    assert ledger.load()["fingerprint"] == decision.fingerprint


def test_delivery_suppresses_unchanged_intraday_snapshot():
    snapshot = _snapshot()
    snapshot.macro_cycle = MacroCycleAssessment(status="insufficient")
    first = decide_delivery(snapshot)
    second = decide_delivery(
        snapshot,
        previous_state={"fingerprint": first.fingerprint, "material_event_ids": first.material_event_ids},
    )
    assert first.mode == "delta"
    assert second.mode == "suppress"
