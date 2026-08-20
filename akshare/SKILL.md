---
name: akshare
description: AKShare 金融数据库接口的检索与执行。AKShare 提供 1000 余个股票、基金、债券、期货、宏观等数据接口，当用户需要从自然语言描述定位接口（"查可转债实时行情"、"股票历史行情怎么取"）、确认某个接口的参数与返回字段、或实际调用接口获取金融数据时使用本技能。典型场景：写金融数据采集代码前先检索接口，执行接口取数并落盘为 CSV/JSON。
---

# AKShare 接口检索与执行

AKShare（1.18.92+）内置完全离线的接口检索层：接口元数据随安装包分发（`akshare/data/interfaces.json`），调用时不发起任何网络请求。检索到接口后的实际取数调用会联网访问数据源。

## 工作流程

取数任务分两步：

1. **检索**：用 `search_interface.py` 从描述定位接口名、确认参数与返回字段。
2. **执行**：用 `call_interface.py` 动态调用接口取数，落盘为 CSV/JSON。

示例：用户要"可转债实时行情" → 检索出 `bond_cb_jsl` → 执行脚本调用并保存。

## 环境

scripts 目录是 uv 管理的独立 Python 项目，依赖 akshare（含 pandas）。所有脚本用 `uv run python` 执行：

```bash
cd scripts
uv run python search_interface.py "可转债 实时行情"
```

## 脚本一：接口检索 search_interface.py

三种模式，均可加 `--json` 输出结构化结果（供管道消费）：

| 模式 | 命令 | 说明 |
|---|---|---|
| 关键词检索 | `python search_interface.py "关键词"` | 按接口名/描述/类目/输出字段做子串匹配，按匹配分降序；支持 `--limit`、`--category`、`--documented-only` |
| 类目列表 | `python search_interface.py --categories` | 列出全部类目及接口数，用于确定 category 取值 |
| 接口元数据 | `python search_interface.py --info <接口名>` | 单接口完整信息：参数（name/type/desc）、输出字段、数据源 URL、调用示例 |

**示例：**

```bash
# 按关键词检索，返回 接口名/类目/描述/有无文档/匹配分 表格
uv run python search_interface.py "可转债 实时行情" --limit 10

# 限定类目检索（先 --categories 看可选类目，如 stock/fund/bond/futures/macro）
uv run python search_interface.py "历史行情" --category stock

# 查单个接口的参数和输出字段（执行前必查，确认参数名）
uv run python search_interface.py --info stock_zh_a_hist

# 结构化输出（JSON），交给 call_interface.py 或程序处理
uv run python search_interface.py "ETF 实时行情" --category fund --json
```

**检索须知（官方能力边界）：**

- 这是关键词子串匹配，不是语义检索："历史行情"匹配不到描述写"历史数据"的接口，一次查询要多试几个说法。
- 传入完整接口名时该接口一定置顶——半记得接口名时用它确认全名。
- 匹配分只供同一次查询内相对排序，不要跨查询比较。
- 部分接口无文档（`有无文档=False`），其描述/参数为空但接口名保证可用；需要完整文档时加 `--documented-only`。
- 元数据是安装版本的快照，升级 akshare 即可刷新。

## 脚本二：接口执行 call_interface.py

```bash
python call_interface.py <接口名> [--params '<JSON参数>'] [--format print|csv|json] [--output <文件>]
```

- `--params`：JSON 对象，参数名以 `--info` 查询结果为准；无参数接口可省略。
- `--format`：默认 `print`（预览前 20 行 + 行数说明）；`csv`/`json` 输出全量。
- `--output`：保存到文件（UTF-8），不指定则输出到标准输出。
- 调用前自动用 `ak.interface_info` 校验接口存在，拼写错误时错误消息会给出最接近的候选名。
- 返回 DataFrame / dict / list 均支持；`None`（空结果）会明确提示。

**示例：**

```bash
# 无参数接口
uv run python call_interface.py energy_carbon_bj

# 带参数，预览结果
uv run python call_interface.py stock_zh_a_hist \
  --params '{"symbol": "600519", "start_date": "20250101", "end_date": "20250201", "adjust": "qfq"}'

# 落盘 CSV
uv run python call_interface.py stock_zh_a_hist \
  --params '{"symbol": "600519", "start_date": "20250101", "end_date": "20250201"}' \
  --format csv --output kline_600519.csv

# 结构化输出（JSON，供程序消费）
uv run python call_interface.py bond_cb_jsl --format json
```

## 脚本三：市场资金流向分析 market_analysis.py

一键输出当日 A 股市场资金流向报告，无需参数：

```bash
uv run python market_analysis.py
```

报告按以下板块顺序输出：

| 板块 | 数据源 | 内容 |
|---|---|---|
| 沪深重要指数 | 新浪（`stock_zh_index_spot_sina`） | 上证/深成/创业板/科创50/沪深300/中证500/中证1000/上证50 行情 + 两市合计成交额 |
| 大盘资金流向 | 东方财富 push2his 直连 | 最新交易日主力/超大单/大单/中单/小单净流入与净占比 |
| 行业资金流 | 同花顺（`stock_fund_flow_industry`） | 净流入 TOP10、净流出 TOP5、全行业汇总 |
| 概念资金流 | 同花顺（`stock_fund_flow_concept`） | 净流入 TOP10、净流出 TOP5 |
| 个股资金流 | 同花顺（`stock_fund_flow_individual`） | 净流入 TOP10、净流出 TOP5（金额统一换算为亿元） |

**要点：**

- 全部取数带自动重试（默认 4 次、间隔 3 秒），数据源偶发失败可自动恢复；单次运行失败会给出明确错误消息。
- AKShare 未提供大盘资金流接口，该板块直连东方财富 push2his 接口补齐，网络或数据源变更时可能失败。
- 同花顺接口返回的个股金额为带"亿/万"后缀的字符串，脚本已统一换算为亿元。
- 行业/概念/个股的净流入 TOP10 与净流出 TOP5 为即时数据，交易日盘中多次运行即可跟踪资金动向变化。

## 通用注意事项

- 执行取数接口会联网访问数据源，可能因网络或数据源变更失败，重试或换接口即可。
- 所有脚本从 scripts 目录执行（`uv run` 自动使用项目虚拟环境）。
- 接口返回字段含义（单位、复权方式等）以 `--info` 的输出字段描述为准。
