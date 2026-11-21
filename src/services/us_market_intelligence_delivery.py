# -*- coding: utf-8 -*-
"""Persistent delivery decisions for full, delta, or suppressed US intelligence notifications."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from src.schemas.us_market_intelligence import DeliveryDecision, USMarketIntelligenceSnapshot


class DeliveryLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def record(self, snapshot: USMarketIntelligenceSnapshot, decision: DeliveryDecision) -> None:
        payload = {
            "snapshot_id": snapshot.snapshot_id,
            "generated_at": snapshot.generated_at,
            "fingerprint": decision.fingerprint,
            "material_event_ids": decision.material_event_ids,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)


def _event_ids(snapshot: USMarketIntelligenceSnapshot) -> list[str]:
    ids = [item.anomaly_id for item in snapshot.anomalies if item.status == "triggered"]
    ids.extend(item.cluster_id for item in snapshot.news_clusters if item.corroboration_status in {"confirmed", "corroborated"})
    ids.extend(item.evidence_id for company in snapshot.company_evidence for item in company.facts)
    return sorted(set(ids))


def decide_delivery(
    snapshot: USMarketIntelligenceSnapshot,
    *,
    previous_state: Optional[dict] = None,
) -> DeliveryDecision:
    events = _event_ids(snapshot)
    fingerprint_material = json.dumps(
        {
            "events": events,
            "risk": snapshot.intelligence.risk_appetite,
            "rotation": snapshot.intelligence.sector_rotation,
            "macro": snapshot.macro_cycle.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(fingerprint_material.encode("utf-8")).hexdigest()
    previous = previous_state or {}
    new_events = sorted(set(events) - set(previous.get("material_event_ids") or []))
    if snapshot.market_session in {"premarket", "postmarket"}:
        return DeliveryDecision(mode="full", reason="盘前/盘后发送完整策略报告", material_event_ids=new_events, fingerprint=fingerprint)
    if previous.get("fingerprint") == fingerprint and not new_events:
        return DeliveryDecision(mode="suppress", reason="本小时无实质主线或事件变化", fingerprint=fingerprint)
    return DeliveryDecision(mode="delta", reason="盘中仅发送新增事件与主线变化", material_event_ids=new_events, fingerprint=fingerprint)
