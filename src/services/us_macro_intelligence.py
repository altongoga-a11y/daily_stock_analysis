# -*- coding: utf-8 -*-
"""Authoritative US macro facts from the public BLS API."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

from src.schemas.us_market_intelligence import MacroCycleAssessment, MacroFact

BLS_ENDPOINT = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_SERIES = {
    "CUUR0000SA0": ("美国CPI", "index", "monthly", "not_seasonally_adjusted"),
    "CUUR0000SA0L1E": ("美国核心CPI", "index", "monthly", "not_seasonally_adjusted"),
    "LNS14000000": ("美国失业率", "percent", "monthly", "seasonally_adjusted"),
    "CES0000000001": ("美国非农就业", "thousand_persons", "monthly", "seasonally_adjusted"),
}


def _evidence_id(series_id: str, period: str, value: float) -> str:
    digest = hashlib.sha256(f"BLS|{series_id}|{period}|{value}".encode("utf-8")).hexdigest()[:16]
    return f"macro-{digest}"


def _period_key(year: str, period: str) -> str:
    return f"{year}-{period.removeprefix('M')}"


def fetch_bls_macro_facts(
    *,
    now: Optional[datetime] = None,
    post: Optional[Callable[..., Any]] = None,
    registration_key: Optional[str] = None,
) -> tuple[list[MacroFact], list[str]]:
    """Fetch key BLS series in one public, keyless-compatible batch request."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload: dict[str, Any] = {
        "seriesid": list(BLS_SERIES),
        "startyear": str(current.year - 2),
        "endyear": str(current.year),
    }
    if registration_key:
        payload["registrationkey"] = registration_key
    requester = post or requests.post
    warnings: list[str] = []
    try:
        response = requester(BLS_ENDPOINT, json=payload, timeout=12)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        return [], [f"BLS 宏观数据不可用：{type(exc).__name__}"]
    if body.get("status") != "REQUEST_SUCCEEDED":
        return [], [f"BLS 请求失败：{'；'.join(body.get('message') or ['unknown'])}"]

    facts: list[MacroFact] = []
    for series in body.get("Results", {}).get("series", []):
        series_id = str(series.get("seriesID") or "")
        definition = BLS_SERIES.get(series_id)
        if definition is None:
            continue
        name, unit, frequency, adjustment = definition
        observations = [item for item in series.get("data", []) if str(item.get("period", "")).startswith("M") and item.get("period") != "M13"]
        observations.sort(key=lambda item: (str(item.get("year")), str(item.get("period"))))
        if not observations:
            warnings.append(f"BLS {series_id} 无可用观察值")
            continue
        latest = observations[-1]
        try:
            value = float(latest["value"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"BLS {series_id} 最新值无效")
            continue
        period = _period_key(str(latest["year"]), str(latest["period"]))
        metadata: dict[str, Any] = {"observations": []}
        for item in observations[-24:]:
            try:
                metadata["observations"].append(
                    {"period": _period_key(str(item["year"]), str(item["period"])), "value": float(item["value"])}
                )
            except (KeyError, TypeError, ValueError):
                continue
        facts.append(
            MacroFact(
                evidence_id=_evidence_id(series_id, period, value),
                series_id=series_id,
                name=name,
                value=value,
                unit=unit,
                period=period,
                frequency=frequency,
                seasonal_adjustment=adjustment,
                provider="U.S. Bureau of Labor Statistics",
                source_url=f"https://data.bls.gov/timeseries/{series_id}",
                retrieved_at=current.isoformat(),
                metadata=metadata,
            )
        )
    return facts, warnings


def _values(fact: Optional[MacroFact]) -> list[float]:
    if fact is None:
        return []
    rows = fact.metadata.get("observations") or fact.metadata.get("latest_three") or []
    return [float(item["value"]) for item in rows if "value" in item]


def fetch_fxmacro_release_calendar(
    *,
    now: Optional[datetime] = None,
    api_key: Optional[str] = None,
    client_factory: Optional[Callable[..., Any]] = None,
) -> tuple[list[str], list[str]]:
    """Return confirmed high-impact USD releases in the next seven days."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        if client_factory is None:
            from fxmacrodata_public import FXMacroDataClient

            client_factory = FXMacroDataClient
        client = client_factory(api_key=api_key or "", timeout=15)
        try:
            payload = client.execute(
                "release_calendar",
                {"currency": "USD", "timezone": "America/New_York"},
            ).as_dict()
        finally:
            client.close()
    except Exception as exc:
        return [], [f"FXMacroData 发布日历不可用：{type(exc).__name__}"]
    data = payload.get("data", {}).get("data", [])
    upcoming: list[str] = []
    for item in data:
        timestamp = item.get("announcement_datetime_utc")
        try:
            release_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        days = (release_at - current).total_seconds() / 86400
        if not 0 <= days <= 7:
            continue
        if not (item.get("top_tier_for_currency") or int(item.get("market_tier") or 99) <= 2):
            continue
        upcoming.append(
            f"{item.get('name') or item.get('release')} | {item.get('announcement_datetime_requested_timezone') or timestamp} | "
            f"importance={item.get('event_importance') or 'unknown'} | source={item.get('source_url') or item.get('source')}"
        )
    return upcoming[:12], []


def assess_macro_cycle(facts: list[MacroFact], *, next_releases: Optional[list[str]] = None) -> MacroCycleAssessment:
    by_id = {fact.series_id: fact for fact in facts}
    required = list(BLS_SERIES)
    missing = [series_id for series_id in required if series_id not in by_id]
    cpi = _values(by_id.get("CUUR0000SA0L1E"))
    unemployment = _values(by_id.get("LNS14000000"))
    payroll = _values(by_id.get("CES0000000001"))

    inflation_direction = "unknown"
    if len(cpi) >= 14 and cpi[-13] != 0 and cpi[-14] != 0:
        recent_yoy = (cpi[-1] / cpi[-13] - 1) * 100
        prior_yoy = (cpi[-2] / cpi[-14] - 1) * 100
        inflation_direction = "falling" if recent_yoy < prior_yoy else "rising" if recent_yoy > prior_yoy else "mixed"

    growth_votes: list[str] = []
    if len(unemployment) >= 3:
        growth_votes.append("falling" if unemployment[-1] > unemployment[-3] else "rising")
    if len(payroll) >= 4:
        recent_gain = payroll[-1] - payroll[-2]
        prior_gain = payroll[-3] - payroll[-4]
        growth_votes.append("rising" if recent_gain >= prior_gain else "falling")
    growth_direction = "unknown"
    if growth_votes:
        growth_direction = growth_votes[0] if len(set(growth_votes)) == 1 else "mixed"

    quadrant = "undetermined"
    if growth_direction == "rising" and inflation_direction == "falling":
        quadrant = "goldilocks"
    elif growth_direction == "rising" and inflation_direction == "rising":
        quadrant = "reflation"
    elif growth_direction == "falling" and inflation_direction == "falling":
        quadrant = "slowdown"
    elif growth_direction == "falling" and inflation_direction == "rising":
        quadrant = "stagflation"
    labels = {
        "goldilocks": "增长改善、通胀回落（Goldilocks）",
        "reflation": "增长改善、通胀上升（Reflation）",
        "slowdown": "增长走弱、通胀回落（Slowdown）",
        "stagflation": "增长走弱、通胀上升（Stagflation）",
        "undetermined": "宏观信号分化，周期未定",
    }
    status = "supported" if not missing and quadrant != "undetermined" else "partial" if facts else "insufficient"
    return MacroCycleAssessment(
        status=status,
        quadrant=quadrant,
        growth_direction=growth_direction,
        inflation_direction=inflation_direction,
        summary=labels[quadrant],
        evidence_ids=[fact.evidence_id for fact in facts],
        missing_series=missing,
        next_releases=list(next_releases or []),
    )
