"""基金持仓分析助手脚本。

输入一个或多个基金代码，自动抓取最新季度持仓、行业配置、
重仓股近期新闻与当日行业资金流，内置规则引擎评估持仓集中度、
行业分布、新闻情绪与行业动量，最后给出操作建议。

用法示例:
    python fund_analysis.py 000001
    python fund_analysis.py 000001 110011 519736
    python fund_analysis.py 000001 --top-n 5
    python fund_analysis.py 000001 --report-only
    python fund_analysis.py 000001 --json
"""

import argparse
import functools
import io
import json
import re
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import ParamSpec, TypeVar

import akshare as ak
import pandas as pd

# 终端表格显示配置：完整显示所有列，中文按等宽对齐
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)
pd.set_option("display.unicode.east_asian_width", True)

# 集中度与行业集中度的评级阈值（占净值比例 %）
CONCENTRATION_HIGH = 40.0
CONCENTRATION_MEDIUM = 25.0
INDUSTRY_HIGH = 30.0

# 新闻情绪关键词（标题与内容任一命中即归类）
POSITIVE_KEYWORDS: tuple[str, ...] = (
    "超预期",
    "增长",
    "增持",
    "回购",
    "中标",
    "订单",
    "突破",
    "创新高",
    "涨停",
    "大涨",
    "盈利",
    "分红",
    "利好",
    "签约",
    "获批",
    "获准",
    "投产",
    "升级",
    "领先",
)
NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "减持",
    "下跌",
    "下滑",
    "亏损",
    "跌停",
    "处罚",
    "违规",
    "立案",
    "暴雷",
    "商誉减值",
    "退市",
    "召回",
    "下调",
    "诉讼",
    "被查",
    "警示",
    "仲裁",
    "裁员",
    "失守",
    "滑坡",
    "暴跌",
    "重挫",
    "大跌",
    "急跌",
    "跳水",
    "腰斩",
    "蒸发",
    "新低",
    "崩了",
    "低迷",
)

# 证监会行业大类 -> 同花顺细分行业关键词（用于行业动量交叉匹配）
INDUSTRY_FLOW_KEYWORDS: dict[str, tuple[str, ...]] = {
    "金融业": ("银行", "证券", "保险", "多元金融"),
    "信息传输、软件和信息技术服务业": (
        "软件开发",
        "互联网服务",
        "通信设备",
        "计算机设备",
        "通信服务",
    ),
    "采矿业": ("煤炭", "石油", "有色金属", "贵金属"),
    "房地产业": ("房地产开发", "房地产服务"),
    "电力、热力、燃气及水生产和供应业": ("电力", "燃气", "公用事业"),
    "交通运输、仓储和邮政业": ("物流", "公路铁路", "航空机场", "航运港口"),
    "批发和零售业": ("商业百货", "贸易", "专业连锁"),
    "科学研究和技术服务业": ("专业服务", "检测"),
    "卫生和社会工作": ("医疗服务", "医疗器械"),
    "文化、体育和娱乐业": ("游戏", "影视", "传媒"),
    "农、林、牧、渔业": ("农牧饲渔", "农业"),
    "建筑业": ("工程建设", "装修建材"),
    "教育": ("教育",),
    "住宿和餐饮业": ("旅游酒店", "餐饮"),
    # 制造业大类过宽，仅匹配主流制造细分行业作参考
    "制造业": (
        "半导体",
        "电子",
        "通信",
        "汽车",
        "家电",
        "白酒",
        "食品",
        "医药",
        "化工",
        "机械",
        "电力设备",
        "光伏",
        "电池",
        "航天",
        "船舶",
        "有色",
        "钢铁",
        "纺织",
        "建材",
    ),
}

# 行业资金流出提示的最低持仓占比阈值（%），避免对零权重行业刷提示
INDUSTRY_FLOW_ALERT_RATIO = 1.0

# 财经事件关键词：命中时在建议中提示关注
EVENT_KEYWORDS: tuple[str, ...] = (
    "中报",
    "年报",
    "季报",
    "财报",
    "业绩预告",
    "股东大会",
)

# 板块/大盘级新闻特征词：此类新闻反映市场整体而非个股，不计入个股正负面
MARKET_LEVEL_KEYWORDS: tuple[str, ...] = ("概念", "板块", "两市", "A股", "大盘", "市场")

P = ParamSpec("P")
T = TypeVar("T")

# 季度列正则，如 "2026年2季度股票投资明细"
_QUARTER_RE = re.compile(r"(\d{4})年(\d+)季度")


@dataclass(frozen=True)
class FundInfo:
    """基金基本信息。"""

    代码: str
    名称: str
    类型: str


@dataclass(frozen=True)
class NewsItem:
    """单条个股新闻。"""

    标题: str
    内容: str
    时间: str
    来源: str


@dataclass
class NewsSentiment:
    """个股新闻情绪统计。"""

    # 裸 list 作为工厂会让字段类型退化为 list[Unknown]，显式参数化保持类型可推导
    正面: list[NewsItem] = field(default_factory=list[NewsItem])
    负面: list[NewsItem] = field(default_factory=list[NewsItem])
    中性数: int = 0

    @property
    def 总数(self) -> int:
        return len(self.正面) + len(self.负面) + self.中性数


@dataclass(frozen=True)
class IndustryAnalysis:
    """单行业的配置占比与今日资金流对照。"""

    行业: str
    占比: float
    资金流入: float | None
    状态: str


@dataclass(frozen=True)
class Concentration:
    """持仓集中度评估。"""

    前五占比: float
    评级: str


@dataclass
class FundData:
    """单只基金的全部原始数据。"""

    基金: FundInfo
    持仓: pd.DataFrame
    行业配置: pd.DataFrame
    新闻: dict[str, list[NewsItem]]
    行业资金流: pd.DataFrame


@dataclass
class FundAnalysis:
    """单只基金的完整分析结果。"""

    基金: FundInfo
    季度: str
    持仓: pd.DataFrame
    行业配置: pd.DataFrame
    集中度: Concentration
    行业分析: list[IndustryAnalysis]
    新闻情绪: dict[str, NewsSentiment]
    建议: list[str]
    结论: str


def retry(
    times: int = 4, delay: float = 3
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """为取数函数添加失败自动重试的装饰器。

    数据源为外部网络接口，偶发超时或限流，重试可显著提高整体成功率；
    达到最大尝试次数仍失败时抛出原始异常。

    Args:
        times: 最大尝试次数。
        delay: 相邻两次尝试的间隔秒数。

    Returns:
        装饰器。
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            for attempt in range(1, times + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    if attempt == times:
                        raise
                    # 间隔重试可避开数据源的瞬时限流
                    print(
                        f"[重试 {attempt}/{times}] {func.__name__}: {type(exc).__name__}",
                        file=sys.stderr,
                    )
                    time.sleep(delay)

            # times 为 0 时循环体不会执行，此处不可达
            raise AssertionError("unreachable")

        return wrapper

    return decorator


def parse_quarter(quarter: str) -> tuple[int, int]:
    """从季度描述文本解析出（年份, 季度序号）。

    Args:
        quarter: 如 "2026年2季度股票投资明细"。

    Returns:
        (年份, 季度序号)，解析失败时返回 (0, 0)。
    """

    match = _QUARTER_RE.search(quarter)
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def latest_quarter_rows(frame: pd.DataFrame, quarter_column: str) -> pd.DataFrame:
    """筛选 DataFrame 中季度最新的数据行。

    季度列可能是 "2026年2季度..." 文本，也可能是 "2026-06-30" 日期，
    统一解析为 (年, 月/季度序号) 后取最大值所在的行组。

    Args:
        frame: 含季度/截止时间列的 DataFrame。
        quarter_column: 季度列名。

    Returns:
        仅保留最新季度数据的行。
    """

    def parse_key(value: object) -> tuple[int, int]:
        text = str(value)
        quarter_match = _QUARTER_RE.search(text)
        if quarter_match:
            return (int(quarter_match.group(1)), int(quarter_match.group(2)))
        date_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if date_match:
            return (int(date_match.group(1)), int(date_match.group(2)))
        return (0, 0)

    # map 的键是 tuple，超出 Series 泛型 dtype 约束，仅注解比较结果收敛 Unknown
    keys = frame[quarter_column].map(parse_key)
    mask: pd.Series[bool] = keys == keys.max()
    return frame.loc[mask].copy()


@retry()
def fetch_fund_name_map() -> dict[str, FundInfo]:
    """获取全市场基金代码到基本信息的映射（天天基金）。

    Returns:
        基金代码到 FundInfo 的字典。
    """

    frame = ak.fund_name_em()
    return {
        row["基金代码"]: FundInfo(row["基金代码"], row["基金简称"], row["基金类型"])
        for row in frame.to_dict("records")
    }


@retry()
def fetch_holdings(symbol: str) -> pd.DataFrame:
    """获取基金最新季度股票持仓（天天基金）。

    Args:
        symbol: 基金代码。

    Returns:
        最新季度持仓表，列含股票代码/股票名称/占净值比例/持股数/持仓市值。
    """

    frame = ak.fund_portfolio_hold_em(symbol=symbol, date="")
    if frame.empty:
        return frame
    return latest_quarter_rows(frame, "季度")


@retry()
def fetch_industry_allocation(symbol: str, year: int) -> pd.DataFrame:
    """获取基金指定年份的行业配置，取最新季度（天天基金）。

    数据源按年份返回全年各季度，年初新数据未披露时逐年前溯。

    Args:
        symbol: 基金代码。
        year: 起始年份。

    Returns:
        最新季度行业配置表，列含行业类别/占净值比例/市值/截止时间。
    """

    for probe_year in range(year, year - 3, -1):
        frame = ak.fund_portfolio_industry_allocation_em(
            symbol=symbol, date=str(probe_year)
        )
        if not frame.empty:
            return latest_quarter_rows(frame, "截止时间")
    return pd.DataFrame()


@retry()
def fetch_news(symbol: str) -> list[NewsItem]:
    """获取个股最近新闻（东方财富，最多 100 条）。

    Args:
        symbol: 股票代码。

    Returns:
        新闻列表，按时间倒序。
    """

    frame = ak.stock_news_em(symbol=symbol)
    if frame.empty:
        return []
    items = [
        NewsItem(row["新闻标题"], row["新闻内容"], row["发布时间"], row["文章来源"])
        for row in frame.to_dict("records")
    ]
    return items


@retry()
def fetch_industry_flow() -> pd.DataFrame:
    """获取当日行业资金流（同花顺，金额单位：亿元）。

    Returns:
        行业资金流表。
    """

    return ak.stock_fund_flow_industry(symbol="即时")


def fetch_top_news(holdings: pd.DataFrame, top_n: int) -> dict[str, list[NewsItem]]:
    """并发抓取前 top_n 大重仓股的新闻。

    Args:
        holdings: 基金持仓表。
        top_n: 抓取新闻的重仓股数量上限。

    Returns:
        股票代码到新闻列表的字典。
    """

    top_holdings = holdings.head(top_n)
    news_map: dict[str, list[NewsItem]] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(fetch_news, row["股票代码"]): row["股票代码"]
            for row in top_holdings.to_dict("records")
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                news_map[symbol] = future.result()
            except Exception as exc:
                # 单只股票新闻失败不阻塞整体分析
                print(f"[警告] {symbol} 新闻抓取失败: {exc}", file=sys.stderr)
                news_map[symbol] = []
    return news_map


def analyze_concentration(holdings: pd.DataFrame) -> Concentration:
    """评估持仓集中度。

    Args:
        holdings: 基金持仓表。

    Returns:
        前五占比与评级（低/中/高）。
    """

    ratios = holdings["占净值比例"].head(5).to_numpy(dtype=float)
    top5_ratio = float(ratios.sum())
    if top5_ratio > CONCENTRATION_HIGH:
        rating = "高"
    elif top5_ratio > CONCENTRATION_MEDIUM:
        rating = "中"
    else:
        rating = "低"
    return Concentration(top5_ratio, rating)


def analyze_industries(
    allocation: pd.DataFrame, flow: pd.DataFrame
) -> list[IndustryAnalysis]:
    """将基金行业配置与当日行业资金流交叉对照。

    证监会行业大类通过关键词映射到同花顺细分行业，
    无法映射的行业（如制造业）标注为无资金流数据。

    Args:
        allocation: 基金行业配置表。
        flow: 当日行业资金流表。

    Returns:
        各行业的占比与资金流对照列表。
    """

    results: list[IndustryAnalysis] = []
    for row in allocation.to_dict("records"):
        industry = row["行业类别"]
        keywords = INDUSTRY_FLOW_KEYWORDS.get(industry)
        if keywords is None:
            results.append(
                IndustryAnalysis(industry, row["占净值比例"], None, "无细分资金流数据")
            )
            continue
        # 命中任一关键词的细分行业净额相加，作为该大类的当日资金流
        matched = flow.loc[flow["行业"].str.contains("|".join(keywords), na=False)]
        if matched.empty:
            results.append(
                IndustryAnalysis(industry, row["占净值比例"], None, "无细分资金流数据")
            )
            continue
        amounts = matched["净额"].to_numpy(dtype=float)
        total_flow = float(amounts.sum())
        status = "净流入" if total_flow > 0 else "净流出"
        results.append(
            IndustryAnalysis(industry, row["占净值比例"], total_flow, status)
        )
    return results


def classify_news(news_list: list[NewsItem], stock_name: str) -> NewsSentiment:
    """按关键词对新闻列表做正面/负面/中性分类。

    Args:
        news_list: 新闻列表。
        stock_name: 股票名称，标题不含该名称的新闻（仅正文提及）不计入个股情绪。

    Returns:
        分类统计结果。
    """

    sentiment = NewsSentiment()
    for item in news_list:
        # 情绪只基于标题判断：正文常含"同比增长"等历史对比词，全文匹配会误判
        title = item.标题
        # 标题不含股票名称的新闻属于他人/板块报道，仅正文提及本股，归为中性
        if stock_name not in title:
            sentiment.中性数 += 1
            continue
        # 板块/大盘级新闻反映市场整体而非个股，归为中性
        if any(keyword in title for keyword in MARKET_LEVEL_KEYWORDS):
            sentiment.中性数 += 1
            continue
        is_positive = any(keyword in title for keyword in POSITIVE_KEYWORDS)
        is_negative = any(keyword in title for keyword in NEGATIVE_KEYWORDS)
        # 正负同时命中时按负面优先，避免"业绩下滑但回购"等场景误报正面
        if is_negative:
            sentiment.负面.append(item)
        elif is_positive:
            sentiment.正面.append(item)
        else:
            sentiment.中性数 += 1
    return sentiment


def analyze_fund(data: FundData) -> FundAnalysis:
    """对单只基金的原始数据做完整规则分析。

    Args:
        data: 基金原始数据。

    Returns:
        完整分析结果。
    """

    concentration = analyze_concentration(data.持仓)
    industry_list = analyze_industries(data.行业配置, data.行业资金流)
    name_by_symbol = dict(zip(data.持仓["股票代码"], data.持仓["股票名称"]))
    sentiment_map = {
        symbol: classify_news(news_list, name_by_symbol.get(symbol, ""))
        for symbol, news_list in data.新闻.items()
    }
    quarter = str(data.持仓["季度"].iloc[0]) if not data.持仓.empty else "未知"
    analysis = FundAnalysis(
        基金=data.基金,
        季度=quarter,
        持仓=data.持仓,
        行业配置=data.行业配置,
        集中度=concentration,
        行业分析=industry_list,
        新闻情绪=sentiment_map,
        建议=[],
        结论="",
    )
    analysis.建议 = generate_suggestions(analysis)
    analysis.结论 = summarize(analysis)
    return analysis


def generate_suggestions(analysis: FundAnalysis) -> list[str]:
    """基于分析结果生成可操作建议。

    Args:
        analysis: 基金分析结果。

    Returns:
        建议列表。
    """

    suggestions: list[str] = []
    concentration = analysis.集中度
    if concentration.评级 == "高":
        suggestions.append(
            f"⚠️ 持仓集中度偏高：前5大重仓股占净值 {concentration.前五占比:.1f}%，"
            "个股波动将显著影响净值，建议关注分散化配置"
        )
    elif concentration.评级 == "中":
        suggestions.append(
            f"持仓集中度中等：前5大重仓股占净值 {concentration.前五占比:.1f}%"
        )

    for industry in analysis.行业分析:
        if industry.占比 >= INDUSTRY_HIGH:
            suggestions.append(
                f"⚠️ 行业集中度高：{industry.行业} 占净值 {industry.占比:.1f}%，单一行业敞口较大"
            )
        amount = industry.资金流入
        if (
            industry.状态 == "净流出"
            and industry.占比 >= INDUSTRY_FLOW_ALERT_RATIO
            and amount is not None
        ):
            suggestions.append(
                f"⚠️ {industry.行业}（占净值 {industry.占比:.1f}%）细分行业今日资金净流出 "
                f"{abs(amount):.1f} 亿元，短期承压"
            )

    for symbol, sentiment in analysis.新闻情绪.items():
        name = analysis.持仓.loc[analysis.持仓["股票代码"] == symbol, "股票名称"].iloc[
            0
        ]
        if len(sentiment.负面) >= 3:
            heads = "、".join(item.标题 for item in sentiment.负面[:2])
            suggestions.append(
                f"⚠️ {name}({symbol}) 近期负面新闻较多（{len(sentiment.负面)} 条），"
                f"包括：{heads}，注意风险"
            )
        elif sentiment.负面 and len(sentiment.负面) >= len(sentiment.正面):
            heads = "、".join(item.标题 for item in sentiment.负面[:2])
            suggestions.append(f"⚠️ {name}({symbol}) 负面新闻：{heads}")
        elif sentiment.正面:
            heads = "、".join(item.标题 for item in sentiment.正面[:2])
            suggestions.append(f"✅ {name}({symbol}) 正面新闻：{heads}")

    # 事件类关键词新闻提示关注（财报、业绩预告等）
    event_hits = [
        item
        for sentiment in analysis.新闻情绪.values()
        for item in sentiment.正面 + sentiment.负面
        if any(keyword in item.标题 for keyword in EVENT_KEYWORDS)
    ]
    if event_hits:
        event_hits.sort(key=lambda item: item.时间, reverse=True)
        latest = event_hits[0]
        suggestions.append(f"📌 关注事件：{latest.时间[:10]} {latest.标题}")

    if not suggestions:
        suggestions.append("持仓结构未见明显风险信号，建议保持现有配置")
    return suggestions


def summarize(analysis: FundAnalysis) -> str:
    """综合各维度信号输出一句话结论。

    Args:
        analysis: 基金分析结果。

    Returns:
        结论文本。
    """

    score = 0
    if analysis.集中度.评级 == "高":
        score -= 1
    score -= sum(1 for industry in analysis.行业分析 if industry.状态 == "净流出")
    total_negative = sum(
        len(sentiment.负面) for sentiment in analysis.新闻情绪.values()
    )
    total_positive = sum(
        len(sentiment.正面) for sentiment in analysis.新闻情绪.values()
    )
    if total_negative >= 3:
        score -= 1
    if total_negative > total_positive:
        score -= 1
    if total_positive >= 3 and total_negative <= 1:
        score += 1
    if score <= -2:
        return "建议谨慎：多重风险信号叠加，注意回撤风险"
    if score == -1:
        return "建议关注：存在一定风险信号，短期谨慎持有"
    return "建议持有：基本面与情绪面未见明显恶化"


def render_fund_report(analysis: FundAnalysis) -> str:
    """渲染单只基金的完整分析报告文本。

    Args:
        analysis: 基金分析结果。

    Returns:
        报告文本。
    """

    fund = analysis.基金
    parts: list[str] = [
        "=" * 70,
        f"【基金概览】{fund.代码} {fund.名称}（{fund.类型}）  持仓季度: {analysis.季度}",
        "",
        "=" * 70,
        "【TOP10 重仓股（占净值比例 % / 持股数 万股 / 市值 万元）】",
    ]
    top_holdings = analysis.持仓.head(10)[
        ["股票代码", "股票名称", "占净值比例", "持股数", "持仓市值"]
    ].copy()
    # 数值列手动格式化为两位小数（pandas 的 float_format 参数类型受限，绕开）
    for column in ["占净值比例", "持股数", "持仓市值"]:
        values = pd.Series(top_holdings[column], dtype=float)
        top_holdings[column] = values.map(lambda value: f"{value:.2f}")
    parts.append(top_holdings.to_string(index=False))

    parts.append("")
    parts.append("=" * 70)
    parts.append("【行业配置（占净值比例 % / 市值 万元）】")
    allocation = analysis.行业配置[
        ["行业类别", "占净值比例", "市值", "截止时间"]
    ].copy()
    for column in ["占净值比例", "市值"]:
        values = pd.Series(allocation[column], dtype=float)
        allocation[column] = values.map(lambda value: f"{value:.2f}")
    parts.append(allocation.to_string(index=False))

    parts.append("")
    parts.append("=" * 70)
    parts.append("【重仓股近期新闻情绪（东方财富）】")
    for symbol, sentiment in analysis.新闻情绪.items():
        name = analysis.持仓.loc[analysis.持仓["股票代码"] == symbol, "股票名称"].iloc[
            0
        ]
        parts.append(
            f"--- {name} ({symbol}): 正面 {len(sentiment.正面)} / 负面 {len(sentiment.负面)} / 中性 {sentiment.中性数}"
        )
        for item in sentiment.正面[:2] + sentiment.负面[:2]:
            parts.append(f"  [{item.时间[:10]}] {item.标题}（{item.来源}）")

    parts.append("")
    parts.append("=" * 70)
    parts.append("【分析评估】")
    parts.append(
        f"★ 持仓集中度: {analysis.集中度.评级}（前5大重仓股占净值 {analysis.集中度.前五占比:.1f}%）"
    )
    for industry in analysis.行业分析:
        flow_text = (
            f"{industry.状态} {abs(industry.资金流入):.1f} 亿元"
            if industry.资金流入 is not None
            else industry.状态
        )
        parts.append(f"★ {industry.行业}: 占净值 {industry.占比:.1f}%，{flow_text}")

    parts.append("")
    parts.append("=" * 70)
    parts.append(f"【操作建议】{analysis.结论}")
    for index, suggestion in enumerate(analysis.建议, start=1):
        parts.append(f"  {index}. {suggestion}")
    return "\n".join(parts)


def to_json(analysis: FundAnalysis) -> dict[str, object]:
    """将分析结果序列化为可 JSON 序列化的字典。

    Args:
        analysis: 基金分析结果。

    Returns:
        结构化分析结果。
    """

    # 仅列出抓取过新闻的重仓股（与新闻情绪字典的键对齐）
    holdings_with_news = analysis.持仓.loc[
        analysis.持仓["股票代码"].isin(analysis.新闻情绪)
    ]
    return {
        "基金代码": analysis.基金.代码,
        "基金名称": analysis.基金.名称,
        "基金类型": analysis.基金.类型,
        "持仓季度": analysis.季度,
        "前五大占比": round(analysis.集中度.前五占比, 2),
        "集中度评级": analysis.集中度.评级,
        "重仓股": [
            {
                "代码": row["股票代码"],
                "名称": row["股票名称"],
                "占净值比例": float(row["占净值比例"]),
                "新闻正面": len(analysis.新闻情绪[row["股票代码"]].正面),
                "新闻负面": len(analysis.新闻情绪[row["股票代码"]].负面),
            }
            for row in holdings_with_news.to_dict("records")
        ],
        "行业配置": [
            {
                "行业": row["行业类别"],
                "占净值比例": float(row["占净值比例"]),
            }
            for row in analysis.行业配置.to_dict("records")
        ],
        "建议": analysis.建议,
        "结论": analysis.结论,
    }


@dataclass(frozen=True)
class AnalyzeOptions:
    """分析任务参数。"""

    fund_names: dict[str, FundInfo]
    top_n: int
    year: int


def analyze_symbols(symbols: list[str], options: AnalyzeOptions) -> list[FundAnalysis]:
    """抓取并分析多只基金。

    Args:
        symbols: 基金代码列表。
        options: 分析任务参数。

    Returns:
        各基金的分析结果列表。
    """

    flow = fetch_industry_flow()
    results: list[FundAnalysis] = []
    for symbol in symbols:
        fund = options.fund_names.get(symbol)
        if fund is None:
            print(
                f"[错误] 未找到基金代码 {symbol}，请通过 fund_name_em 确认代码",
                file=sys.stderr,
            )
            continue
        holdings = fetch_holdings(symbol)
        if holdings.empty:
            print(f"[错误] 基金 {symbol} 暂无持仓数据", file=sys.stderr)
            continue
        allocation = fetch_industry_allocation(symbol, options.year)
        news_map = fetch_top_news(holdings, options.top_n)
        data = FundData(fund, holdings, allocation, news_map, flow)
        results.append(analyze_fund(data))
    return results


def main() -> None:
    """命令行入口，解析参数并输出分析报告。"""

    # Windows 终端默认 GBK 编码，重配为标准 UTF-8 保证中文正常显示
    # 先落局部变量再收窄，避免对 sys 模块成员收窄引入 Unknown 类型参数
    stdout = sys.stdout
    stderr = sys.stderr
    if isinstance(stdout, io.TextIOWrapper):
        stdout.reconfigure(encoding="utf-8")
    if isinstance(stderr, io.TextIOWrapper):
        stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="基金持仓分析助手")
    parser.add_argument("codes", nargs="+", help="基金代码，可传多个")
    parser.add_argument(
        "--top-n", type=int, default=10, help="抓取新闻的重仓股数量上限（默认 10）"
    )
    parser.add_argument(
        "--year",
        type=int,
        default=datetime.now().astimezone().date().year,
        help="行业配置年份（默认当前年份）",
    )
    parser.add_argument(
        "--report-only", action="store_true", help="仅输出数据报告，不做分析建议"
    )
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON 结果")
    args = parser.parse_args()

    try:
        options = AnalyzeOptions(fetch_fund_name_map(), args.top_n, args.year)
        results = analyze_symbols(args.codes, options)
        if args.json:
            print(
                json.dumps(
                    [to_json(result) for result in results],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            for result in results:
                report = render_fund_report(result)
                if args.report_only:
                    # 去掉建议板块后仅展示数据
                    report = report.split("【操作建议】")[0].rstrip()
                print(report)
                print()
    except Exception as exc:
        # CLI 入口统一兜底：数据源异常转为友好消息
        print(f"基金分析失败: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
