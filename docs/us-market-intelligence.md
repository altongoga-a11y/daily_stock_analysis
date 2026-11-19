# 美股每小时情报

`美股每小时情报` 是一个独立于原“每日股票分析”的 GitHub Actions 工作流。它面向美股盘前、盘中与盘后情报，不会每小时重复执行完整个股分析。

## 覆盖内容

- 核心指数代理：SPY、QQQ、DIA、IWM 与 VIX
- 11 个 GICS 行业 ETF：XLC、XLY、XLP、XLE、XLF、XLV、XLI、XLB、XLRE、XLK、XLU
- 跨资产线索：美国 10 年期国债收益率、美元指数、黄金和 WTI 原油
- `US_INTELLIGENCE_WATCHLIST` 或既有 `STOCK_LIST` 中的大盘股
- Brave Search 返回的近期市场主线、板块轮动、美国经济日历与重要财报新闻
- OpenAI/LiteLLM 生成的中文情报摘要和未来 1–3 小时观察项

## GitHub 配置

复用既有 Secrets：

- `OPENAI_API_KEY`
- `BRAVE_API_KEYS`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

可选 Repository Variable：

```text
US_INTELLIGENCE_WATCHLIST=AAPL,MSFT,NVDA,GOOGL,AMZN,META,JPM,XOM,LLY,WMT,CAT,NEE
```

未设置时会读取既有 `STOCK_LIST`。如需指定 OpenAI 模型，可设置 `OPENAI_MODEL`；不应把 API Key 放入 Repository Variables。

## 调度

工作流文件为 `.github/workflows/05-us-market-intelligence.yml`。GitHub cron 每个工作日按 UTC 触发，脚本再使用 `America/New_York` 时区和 NYSE 交易日历判断是否运行，因此兼容美国夏令时与常见休市日。

有效窗口为纽约时间 07:00–16:59，覆盖盘前、盘中每小时和收盘后的快照。GitHub Actions 的定时触发可能延迟数分钟，不适用于秒级交易信号。

首次部署后，在 GitHub 的 **Actions → 美股每小时情报 → Run workflow** 手动运行。可以选择只生成 artifact、不发送 Telegram。

## 本地验证

离线样例不会调用行情、搜索、LLM 或 Telegram：

```bash
python scripts/us_market_intelligence.py --sample --no-send
```

真实运行但不发送 Telegram：

```bash
python scripts/us_market_intelligence.py --no-send
```

## 数据边界

- 行情来自 Yahoo Finance 的最新可用数据，可能延迟、缺字段或受限流影响，不是交易所直连实时行情。
- 行业轮动基于 11 个行业 ETF 相对 SPY 的当日涨跌表现，是价格强弱代理，不等于完整资金流统计。
- Brave Search 用于发现新闻线索。搜索标题和摘要不能替代 SEC 文件、公司投资者关系公告、BLS、BEA、Federal Reserve 等原始来源。
- 任一行情、新闻或 LLM 环节失败时，脚本保留其他可用数据；LLM 失败会退回确定性 Markdown 报告。

## 回滚

禁用或删除 `.github/workflows/05-us-market-intelligence.yml` 即可停止小时情报，不影响原 `00-daily-analysis.yml`。新增服务和脚本没有数据库迁移，也不会改变原每日工作流的默认行为。
