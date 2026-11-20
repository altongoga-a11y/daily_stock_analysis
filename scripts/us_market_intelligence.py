# -*- coding: utf-8 -*-
"""Run one US market intelligence snapshot and optionally push it to Telegram."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.us_market_intelligence import (  # noqa: E402
    NEW_YORK,
    configured_watchlist_from_env,
    should_run_scheduled,
)
from src.services.us_market_intelligence_engine import run_intelligence_engine  # noqa: E402
from src.services.us_market_intelligence_history import USMarketIntelligenceHistory  # noqa: E402

logger = logging.getLogger("us_market_intelligence")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an hourly US market intelligence report")
    parser.add_argument("--scheduled", action="store_true", help="Skip outside useful NYSE hours or on market holidays")
    parser.add_argument("--no-send", action="store_true", help="Generate the report without Telegram delivery")
    parser.add_argument("--sample", action="store_true", help="Use deterministic offline sample data and no external APIs")
    parser.add_argument("--output", default="reports/us-market-intelligence.md", help="Markdown output path")
    parser.add_argument("--json-output", default="reports/us-market-intelligence.json", help="Structured JSON output path")
    parser.add_argument("--history-dir", default="data/us-market-intelligence", help="Persistent snapshot directory")
    return parser.parse_args()


def _sample_download(symbols, *, interval, **_kwargs):
    tickers = list(symbols) if not isinstance(symbols, str) else [symbols]
    now = pd.Timestamp("2026-11-19 15:00:00", tz="America/New_York")
    if interval == "5m":
        index = pd.DatetimeIndex([now - pd.Timedelta(minutes=5), now])
        frames = {
            symbol: pd.DataFrame(
                {"Close": [100.0 + index_pos, 101.0 + index_pos], "Volume": [1000, 1200]},
                index=index,
            )
            for index_pos, symbol in enumerate(tickers)
        }
    else:
        index = pd.DatetimeIndex([now.normalize() - pd.Timedelta(days=2), now.normalize() - pd.Timedelta(days=1)])
        frames = {
            symbol: pd.DataFrame(
                {"Close": [99.0 + index_pos, 100.0 + index_pos], "Volume": [900, 1000]},
                index=index,
            )
            for index_pos, symbol in enumerate(tickers)
        }
    return pd.concat(frames, axis=1)


def _sample_config():
    return SimpleNamespace(stock_list=["AAPL", "MSFT", "NVDA"], brave_api_keys=[])


def _generate_analysis(config, prompt: str) -> tuple[str, str, str]:
    from src.analyzer import GeminiAnalyzer

    analyzer = GeminiAnalyzer(config=config)
    result = analyzer.generate_text_with_metadata(prompt, max_tokens=2200, temperature=0.2)
    if result is None or not result.text.strip():
        return "", "", ""
    return result.text.strip(), result.provider or "", result.model or ""


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    now = datetime(2026, 11, 19, 15, 0, tzinfo=NEW_YORK) if args.sample else datetime.now(tz=NEW_YORK)
    if args.scheduled:
        allowed, reason = should_run_scheduled(now)
        if not allowed:
            logger.info("Skipping scheduled US intelligence run: %s", reason)
            return 0

    if args.sample:
        config = _sample_config()
        watchlist = config.stock_list
        generator = None
        sample_download = _sample_download
    else:
        from src.config import get_config

        config = get_config()
        watchlist = configured_watchlist_from_env() or list(getattr(config, "stock_list", []) or [])

        def generator(prompt: str) -> tuple[str, str, str]:
            return _generate_analysis(config, prompt)

        sample_download = None

    history = USMarketIntelligenceHistory(PROJECT_ROOT / args.history_dir)
    result = run_intelligence_engine(
        config,
        watchlist=watchlist,
        now=now,
        download=sample_download,
        generate=generator,
        history=history,
        validate_news_links=not args.sample,
    )
    report = result.report
    generation = "/".join(
        part for part in (result.snapshot.intelligence.provider, result.snapshot.intelligence.model) if part
    ) or result.snapshot.intelligence.status
    report += f"\n\n<!-- generation: {generation} -->\n"
    result.snapshot.report_markdown = report

    output_path = PROJECT_ROOT / args.output
    json_output_path = PROJECT_ROOT / args.json_output
    _atomic_write_text(output_path, report)
    _atomic_write_text(
        json_output_path,
        json.dumps(result.snapshot.to_json_dict(), ensure_ascii=False, indent=2, sort_keys=True),
    )
    logger.info("Reports written to %s and %s", output_path, json_output_path)

    summary = {
        "snapshot_id": result.snapshot.snapshot_id,
        "session": result.data.session_label,
        "quotes": len(result.data.quotes),
        "news": len(result.data.news),
        "anomalies": len(result.snapshot.anomalies),
        "previous_hour": result.snapshot.comparison.previous_hour.status,
        "previous_trading_day": result.snapshot.comparison.previous_trading_day.status,
        "data_quality": result.snapshot.data_quality.overall_status,
        "warnings": result.data.warnings,
        "generation": generation,
        "output": str(output_path),
        "json_output": str(json_output_path),
        "history_path": str(result.history_path) if result.history_path else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.sample or args.no_send:
        return 0

    from src.notification_sender.telegram_sender import TelegramSender

    if not TelegramSender(config).send_to_telegram(report, timeout_seconds=15):
        logger.error("Telegram delivery failed")
        return 2
    logger.info("Telegram delivery succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
