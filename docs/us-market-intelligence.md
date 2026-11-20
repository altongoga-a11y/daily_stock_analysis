# 美股每小时情报

`美股每小时情报` 是一个独立于原“每日股票分析”的 GitHub Actions 工作流。它面向美股盘前、盘中与盘后情报，不会每小时重复执行完整个股分析。

## 覆盖内容

- 核心指数代理：SPY、QQQ、DIA、IWM 与 VIX
- 11 个 GICS 行业 ETF：XLC、XLY、XLP、XLE、XLF、XLV、XLI、XLB、XLRE、XLK、XLU
- 跨资产线索：美国 10 年期国债收益率、美元指数、黄金和 WTI 原油
- `US_INTELLIGENCE_WATCHLIST` 或既有 `STOCK_LIST` 中的大盘股
- Brave Search 返回的近期市场主线、板块轮动、美国经济日历与重要财报新闻
- OpenAI/LiteLLM 生成的中文情报摘要和未来 1–3 小时观察项
- 每条行情的来源、provider 时间、纽约市场时间、交易时段、TTL 与校验状态
- 上一小时和上一交易日变化、确定性异常预警及证据引用
- 1日、5日、1个月、3个月、6个月行业相对 SPY 轮动与条件情景
- 经济周期、金融主线、主要矛盾、跨资产验证和相对强势观察股
- 逐条合格新闻的结论、潜在影响、主线映射与验证要求
- 同快照 ID 的中文 Markdown 与供其他机器人调用的结构化 JSON

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

未设置时会读取既有 `STOCK_LIST`。工作流对官方 OpenAI Key 默认使用 `gpt-4o-mini`；如需其他模型，可通过 Repository Variable `OPENAI_MODEL` 覆盖。API Key 不应放入 Repository Variables。系统先使用项目 LiteLLM 路由，失败时再使用官方 OpenAI SDK 直连；两条路径的模型不可用、权限、余额或配额错误会记录在报告数据边界和 JSON warnings 中，确定性策略报告仍会生成。

## 调度

工作流文件为 `.github/workflows/05-us-market-intelligence.yml`。GitHub cron 每个工作日按 UTC 触发，脚本再使用 `America/New_York` 时区和 NYSE 交易日历判断是否运行，因此兼容美国夏令时与常见休市日。

有效窗口为纽约时间 07:00–16:59，覆盖盘前、盘中每小时和收盘后的快照。GitHub Actions 的定时触发可能延迟数分钟，不适用于秒级交易信号。

首次部署后，在 GitHub 的 **Actions → 美股每小时情报 → Run workflow** 手动运行。可以选择只生成 artifact、不发送 Telegram。

工作流会从最近一次成功运行的 `us-market-intelligence-state` artifact 恢复历史快照，并在成功后保存新状态。因此 GitHub 托管 Runner 也能比较上一小时和上一交易日。第一次运行、artifact 过期或恢复失败时，当次报告仍会生成，但比较状态会明确标记为 `no_baseline`。

## 输出契约

- 人类报告：`reports/us-market-intelligence.md`
- 机器人 JSON：`reports/us-market-intelligence.json`
- 历史快照：`data/us-market-intelligence/snapshots/YYYY-MM-DD/<snapshot_id>.json`

JSON 使用版本化严格 Schema。行情事实固定标记为 `confirmed_fact`；规则计算结论为 `data_inference`；新闻潜在驱动为 `unverified_hypothesis`。新闻与价格同时出现不代表因果，AI Prompt 和结构化字段均禁止自动确认因果关系。

## 本地验证

离线样例不会调用行情、搜索、LLM 或 Telegram：

```bash
python scripts/us_market_intelligence.py --sample --no-send
```

真实运行但不发送 Telegram：

```bash
python scripts/us_market_intelligence.py --no-send
```

离线样例会同时生成 Markdown、JSON 和版本化快照；重复运行可以验证基线读取与原子覆盖。

## 数据边界

- 行情来自 Yahoo Finance 的最新可用数据，可能延迟、缺字段或受限流影响，不是交易所直连实时行情。盘中默认 15 分钟 TTL；盘后已完成交易时段的数据按隔夜有效处理，避免把正常收盘数据误报为陈旧。
- 行业轮动基于 11 个行业 ETF 相对 SPY 的当日涨跌表现，是价格强弱代理，不等于完整资金流统计。
- Brave Search 用于发现新闻线索。每条结果会检查 URL 格式、HTTP 可达性和发布时间；搜索结果缺时间时会尝试从页面 `article:published_time` / `datePublished` 元数据恢复，但这些检查不能替代 SEC 文件、公司投资者关系公告、BLS、BEA、Federal Reserve 等原始来源。
- 新闻链接缺时间、不可达、过期或时间在未来时会降级对应证据，不会阻断整份报告。
- 任一行情、新闻、历史恢复或 LLM 环节失败时，脚本保留其他可用数据；LLM 失败会退回确定性 Markdown 与 JSON 输出。

## 回滚

禁用或删除 `.github/workflows/05-us-market-intelligence.yml` 即可停止小时情报，不影响原 `00-daily-analysis.yml`。新增服务和脚本没有数据库迁移，也不会改变原每日工作流的默认行为。
