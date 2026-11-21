# -*- coding: utf-8 -*-
"""Deterministic source assessment and event clustering for US news evidence."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.schemas.us_market_intelligence import NewsCluster, NewsEvidence

_OFFICIAL = {"sec.gov", "bls.gov", "bea.gov", "federalreserve.gov"}
_TIER1 = {"reuters.com", "apnews.com", "bloomberg.com", "wsj.com", "ft.com"}
_TIER2 = {"cnbc.com", "marketwatch.com", "barrons.com", "finance.yahoo.com", "schwab.com"}
_TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}
_STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "today", "stock", "market"}


def canonicalize_news_url(url: str) -> str:
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in _TRACKING_KEYS]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower().removeprefix("www."), parsed.path.rstrip("/"), "", urlencode(query), ""))


def source_tier(domain: str) -> str:
    normalized = domain.lower().removeprefix("www.")
    if any(normalized == item or normalized.endswith(f".{item}") for item in _OFFICIAL):
        return "official"
    if any(normalized == item or normalized.endswith(f".{item}") for item in _TIER1):
        return "tier1"
    if any(normalized == item or normalized.endswith(f".{item}") for item in _TIER2):
        return "tier2"
    return "tier3"


def _tokens(title: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", title.lower()) if len(token) >= 3 and token not in _STOPWORDS}


def _topic(evidence: NewsEvidence) -> str:
    text = f"{evidence.title} {evidence.snippet}".lower()
    mapping = (
        ("monetary_policy", ("fed", "rate", "yield", "inflation", "cpi", "pce")),
        ("earnings", ("earnings", "revenue", "eps", "guidance", "profit")),
        ("geopolitics", ("war", "iran", "china", "tariff", "sanction")),
        ("technology_ai", ("ai", "semiconductor", "chip", "technology")),
        ("energy", ("oil", "opec", "energy", "natural gas")),
    )
    for topic, terms in mapping:
        if any(term in text for term in terms):
            return topic
    return evidence.category or "market"


def cluster_news(evidence: list[NewsEvidence]) -> list[NewsCluster]:
    groups: list[list[NewsEvidence]] = []
    for item in evidence:
        item_tokens = _tokens(item.title)
        placed = False
        for group in groups:
            exemplar_tokens = _tokens(group[0].title)
            union = item_tokens | exemplar_tokens
            similarity = len(item_tokens & exemplar_tokens) / len(union) if union else 0.0
            if _topic(item) == _topic(group[0]) and similarity >= 0.35:
                group.append(item)
                placed = True
                break
        if not placed:
            groups.append([item])

    clusters: list[NewsCluster] = []
    for group in groups:
        domains = sorted({urlparse(canonicalize_news_url(item.url)).hostname or item.source for item in group})
        tiers = [source_tier(domain) for domain in domains]
        primary = "official" in tiers
        best_tier = min(tiers, key=("official", "tier1", "tier2", "tier3").index)
        independent = len(domains)
        if primary:
            status = "confirmed"
        elif independent >= 2 and best_tier in {"tier1", "tier2"}:
            status = "corroborated"
        elif independent == 1:
            status = "single_source"
        else:
            status = "unverified"
        identity = "|".join(sorted(item.evidence_id for item in group))
        cluster_id = "news-cluster-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        published = sorted(item.published_at for item in group if item.published_at)
        clusters.append(
            NewsCluster(
                cluster_id=cluster_id,
                topic=_topic(group[0]),
                canonical_title=group[0].title,
                member_evidence_ids=[item.evidence_id for item in group],
                source_domains=domains,
                source_tier=best_tier,
                independent_source_count=independent,
                primary_source_present=primary,
                corroboration_status=status,
                first_seen_at=published[0] if published else None,
                last_seen_at=published[-1] if published else None,
                summary=group[0].snippet,
            )
        )
    return sorted(
        clusters,
        key=lambda item: (
            ("confirmed", "corroborated", "single_source", "unverified").index(item.corroboration_status),
            -item.independent_source_count,
        ),
    )
