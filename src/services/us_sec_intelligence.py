# -*- coding: utf-8 -*-
"""Minimal, compliant SEC EDGAR evidence client for US market intelligence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from src.schemas.us_market_intelligence import CompanyEvidence, SecFact

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_ALLOWED_FORMS = {"10-K", "10-Q", "8-K", "20-F", "40-F", "6-K"}
_CONCEPTS = {
    "Revenues": "营收",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "客户合同营收",
    "NetIncomeLoss": "净利润",
    "OperatingIncomeLoss": "营业利润",
    "NetCashProvidedByUsedInOperatingActivities": "经营现金流",
}


class SecEdgarClient:
    def __init__(
        self,
        user_agent: str,
        *,
        get: Optional[Callable[..., Any]] = None,
        cache_dir: str | Path = "data/us-market-intelligence/sec-cache",
        cache_ttl_seconds: int = 24 * 60 * 60,
    ):
        value = (user_agent or "").strip()
        if not value or "@" not in value:
            raise ValueError("SEC_USER_AGENT 必须包含应用名称和联系邮箱")
        self.user_agent = value
        self._get = get or requests.get
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._ticker_cache: Optional[dict[str, str]] = None
        self.cache_dir = Path(cache_dir)
        self.cache_ttl_seconds = max(60, int(cache_ttl_seconds))

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.json"

    def _read_cache(self, url: str) -> Optional[Any]:
        path = self._cache_path(url)
        try:
            if time.time() - path.stat().st_mtime > self.cache_ttl_seconds:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

    def _write_cache(self, url: str, payload: Any) -> None:
        path = self._cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def _request_json(self, url: str) -> Any:
        cached = self._read_cache(url)
        if cached is not None:
            return cached
        with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < 0.12:
                time.sleep(0.12 - elapsed)
            response = self._get(
                url,
                headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
                timeout=12,
            )
            self._last_request_at = time.monotonic()
        response.raise_for_status()
        payload = response.json()
        self._write_cache(url, payload)
        return payload

    def resolve_cik(self, ticker: str) -> Optional[str]:
        if self._ticker_cache is None:
            payload = self._request_json(SEC_TICKERS_URL)
            self._ticker_cache = {
                str(item.get("ticker", "")).upper(): str(item.get("cik_str", "")).zfill(10)
                for item in payload.values()
                if isinstance(item, dict)
            }
        return self._ticker_cache.get(ticker.upper())

    def company_evidence(self, ticker: str) -> CompanyEvidence:
        try:
            cik = self.resolve_cik(ticker)
            if not cik:
                return CompanyEvidence(ticker=ticker, status="unavailable", warnings=["SEC CIK 未找到"])
            submissions = self._request_json(SEC_SUBMISSIONS_URL.format(cik=cik))
            companyfacts = self._request_json(SEC_FACTS_URL.format(cik=cik))
        except Exception as exc:
            return CompanyEvidence(ticker=ticker, status="unavailable", warnings=[f"SEC 请求失败：{type(exc).__name__}"])

        recent = submissions.get("filings", {}).get("recent", {})
        forms = [str(value) for value in recent.get("form", []) if str(value) in _ALLOWED_FORMS][:10]
        facts: list[SecFact] = []
        us_gaap = companyfacts.get("facts", {}).get("us-gaap", {})
        for concept, label in _CONCEPTS.items():
            raw = us_gaap.get(concept)
            if not isinstance(raw, dict):
                continue
            for unit, entries in raw.get("units", {}).items():
                candidates = [item for item in entries if item.get("form") in {"10-K", "10-Q"} and item.get("filed")]
                candidates.sort(key=lambda item: (str(item.get("filed")), str(item.get("end"))))
                if not candidates:
                    continue
                item = candidates[-1]
                try:
                    value = float(item["val"])
                except (KeyError, TypeError, ValueError):
                    continue
                accession = str(item.get("accn") or "")
                evidence_id = "sec-" + hashlib.sha256(
                    f"{ticker}|{concept}|{accession}|{value}".encode("utf-8")
                ).hexdigest()[:16]
                filing_url = (
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{re.sub(r'[^0-9]', '', accession)}/"
                    if accession
                    else "https://www.sec.gov/edgar/search/"
                )
                facts.append(
                    SecFact(
                        evidence_id=evidence_id,
                        ticker=ticker,
                        cik=cik,
                        concept=concept,
                        label=label,
                        value=value,
                        unit=str(unit),
                        period_end=str(item.get("end") or ""),
                        filed_at=str(item.get("filed") or ""),
                        form=str(item.get("form") or ""),
                        accession=accession,
                        source_url=filing_url,
                    )
                )
                break
        return CompanyEvidence(
            ticker=ticker,
            status="available" if facts else "partial",
            recent_forms=forms,
            facts=facts,
            warnings=[] if facts else ["SEC companyfacts 未匹配核心财务概念"],
        )


def collect_sec_evidence(watchlist: list[str], user_agent: Optional[str]) -> list[CompanyEvidence]:
    if not (user_agent or "").strip():
        return [CompanyEvidence(ticker=ticker, status="disabled", warnings=["未配置 SEC_USER_AGENT"]) for ticker in watchlist]
    try:
        client = SecEdgarClient(user_agent or "")
    except ValueError as exc:
        return [CompanyEvidence(ticker=ticker, status="disabled", warnings=[str(exc)]) for ticker in watchlist]
    return [client.company_evidence(ticker) for ticker in watchlist]
