# -*- coding: utf-8 -*-
"""Atomic file history for portable US market intelligence snapshots."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from src.schemas.us_market_intelligence import USMarketIntelligenceSnapshot


class USMarketIntelligenceHistory:
    def __init__(self, root: str | Path = "data/us-market-intelligence", *, retention_days: int = 30):
        self.root = Path(root)
        self.snapshots_dir = self.root / "snapshots"
        self.latest_path = self.root / "latest.json"
        self.retention_days = max(1, int(retention_days))

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _load(path: Path) -> Optional[USMarketIntelligenceSnapshot]:
        try:
            return USMarketIntelligenceSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

    def save(self, snapshot: USMarketIntelligenceSnapshot) -> Path:
        generated = datetime.fromisoformat(snapshot.generated_at_new_york)
        day_dir = self.snapshots_dir / generated.strftime("%Y-%m-%d")
        path = day_dir / f"{snapshot.snapshot_id}.json"
        payload = snapshot.to_json_dict()
        if not path.exists():
            self._atomic_write_json(path, payload)
        self._atomic_write_json(self.latest_path, payload)
        self.apply_retention(now=generated)
        return path

    def list_snapshots(self, *, before: Optional[datetime] = None) -> list[USMarketIntelligenceSnapshot]:
        if not self.snapshots_dir.exists():
            return []
        snapshots: list[USMarketIntelligenceSnapshot] = []
        before_utc = before.astimezone().timestamp() if before is not None else None
        for path in self.snapshots_dir.glob("*/*.json"):
            snapshot = self._load(path)
            if snapshot is None:
                continue
            try:
                generated = datetime.fromisoformat(snapshot.generated_at)
            except ValueError:
                continue
            if before_utc is not None and generated.timestamp() >= before_utc:
                continue
            snapshots.append(snapshot)
        return sorted(snapshots, key=lambda item: item.generated_at, reverse=True)

    def baselines(
        self,
        current_time: datetime,
    ) -> tuple[Optional[USMarketIntelligenceSnapshot], Optional[USMarketIntelligenceSnapshot]]:
        snapshots = self.list_snapshots(before=current_time)
        previous_hour = snapshots[0] if snapshots else None
        current_local_date = current_time.date()
        previous_day = next(
            (
                snapshot
                for snapshot in snapshots
                if datetime.fromisoformat(snapshot.generated_at_new_york).date() < current_local_date
            ),
            None,
        )
        return previous_hour, previous_day

    def apply_retention(self, *, now: datetime) -> None:
        cutoff = now.date() - timedelta(days=self.retention_days)
        if not self.snapshots_dir.exists():
            return
        for day_dir in self.snapshots_dir.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day >= cutoff:
                continue
            for path in day_dir.glob("*.json"):
                path.unlink(missing_ok=True)
            try:
                day_dir.rmdir()
            except OSError:
                pass
