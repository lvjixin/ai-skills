"""板块轮动分析与基金推荐脚本。

基于近 1 个月的行业资金流（同花顺 20 日排行）、近 1 个月的新闻联播文字稿
与最新披露期业绩报表（东方财富）的行业业绩，对同花顺 90 个行业板块做
资金 / 情绪 / 基本面三维打分，输出未来值得关注的板块、建议回避的板块，
并从全部关注板块中挑选近期动量最强的几只场外基金与 ETF 作为购入参考。

用法示例:
    python sector_rotation.py
    python sector_rotation.py --top-n 8 --bottom-n 5
    python sector_rotation.py --quarter 20260630
    python sector_rotation.py --json
"""

import argparse
import functools
import io
import json
import math
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import ParamSpec, TypeVar

import akshare as ak
import pandas as pd

# akshare/__init__.py 过大导致 pyright 对部分符号解析失效，
# 这几个接口改为从子模块直接导入以保证类型可推导
from akshare.fund.fund_rank_em import (
    fund_exchange_rank_em as fetch_etf_rank_raw,
)
from akshare.fund.fund_rank_em import (
    fund_open_fund_rank_em as fetch_fund_rank_raw,
)
from akshare.news.news_cctv import (
    news_cctv as fetch_news_cctv_api,
)
from akshare.stock_feature.stock_yjbb_em import (
    stock_yjbb_em as fetch_earnings_api,
)

# 终端表格显示配置：完整显示所有列，中文按等宽对齐
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)
pd.set_option("display.unicode.east_asian_width", True)

# 评分权重：20日资金 40 + 当日资金 10 + 新闻情绪 20 + 财报基本面 30，总分 100
WEIGHT_FLOW_20D = 40.0
WEIGHT_FLOW_TODAY = 10.0
WEIGHT_NEWS = 20.0
WEIGHT_EARNINGS = 30.0

# 板块定性阈值：>=65 重点关注，>=45 中性观望，其余建议回避
WATCH_THRESHOLD = 65.0
NEUTRAL_THRESHOLD = 45.0

# 业绩报表最少披露家数，低于视为披露期未展开，逐季前溯
MIN_EARNINGS_ROWS = 300

# 财报净利增速中位数到基本面得分的线性映射：-50%→0 分、0%→15 分、+50% 及以上→满分
EARNINGS_FULL_GROWTH = 50.0
EARNINGS_ZERO_SCORE = 15.0

# 新闻情绪基准分与每条净正负面的加减分
NEWS_BASE_SCORE = 10.0
NEWS_STEP_SCORE = 2.0

# 全部关注板块合计推荐的基金/ETF 总数
TOTAL_FUND_PICKS = 8

# 新闻回溯交易日天数
NEWS_LOOKBACK_DAYS = 22

# 报告头条区展示的重要快讯条数
HEADLINE_LIMIT = 6

# 快讯正负面关键词（标题与摘要合并文本任一命中即归类，正负同现按负面优先）
POSITIVE_KEYWORDS: tuple[str, ...] = (
    "增长",
    "超预期",
    "利好",
    "突破",
    "创新高",
    "大涨",
    "涨停",
    "盈利",
    "回升",
    "扩张",
    "加码",
    "获批",
    "中标",
    "签约",
    "回购",
    "增持",
    "降准",
    "降息",
    "减税",
    "补贴",
    "规划",
    "振兴",
    "刺激",
)
NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "下跌",
    "下滑",
    "亏损",
    "跌停",
    "处罚",
    "违规",
    "立案",
    "减持",
    "下调",
    "制裁",
    "关税",
    "加征",
    "限制",
    "收紧",
    "衰退",
    "暴跌",
    "重挫",
    "大跌",
    "新低",
    "风险",
    "警示",
    "裁员",
    "退市",
)

# 重要快讯筛选词：命中即进入报告头条区
HEADLINE_KEYWORDS: tuple[str, ...] = (
    "重磅",
    "突发",
    "国务院",
    "央行",
    "证监会",
    "财政部",
    "发改委",
    "工信部",
    "政策",
    "规划",
    "降准",
    "降息",
    "关税",
    "制裁",
)

# 同花顺板块名 -> 快讯搜索词（无映射的板块用板块名本身兜底）
SECTOR_NEWS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "半导体": ("半导体", "芯片"),
    "软件开发": ("软件", "信创", "人工智能"),
    "互联网服务": ("互联网", "电商"),
    "计算机设备": ("计算机", "服务器"),
    "通信设备": ("通信", "5G", "算力"),
    "通信服务": ("通信", "运营商"),
    "银行": ("银行", "信贷"),
    "保险": ("保险", "保费"),
    "证券": ("证券", "券商", "资本市场"),
    "白酒": ("白酒", "酒企"),
    "食品加工": ("食品", "乳制品"),
    "医药商业": ("医药", "药店"),
    "医疗": ("医疗", "医院", "创新药"),
    "医疗器械": ("医疗器械", "医疗"),
    "生物制品": ("疫苗", "创新药", "生物医药"),
    "化学制药": ("创新药", "药品"),
    "有色金属": ("有色", "铜价", "铝价"),
    "贵金属": ("黄金", "白银", "贵金属"),
    "小金属": ("稀土", "钨", "锑"),
    "能源金属": ("锂价", "钴", "镍"),
    "光伏设备": ("光伏", "组件"),
    "电池": ("电池", "锂电", "储能"),
    "电网设备": ("电网", "特高压"),
    "电力": ("电力", "用电量", "电价"),
    "汽车整车": ("汽车", "新能源车", "车企"),
    "汽车服务": ("汽车", "车企"),
    "航天航空": ("军工", "航空航天", "大飞机"),
    "船舶制造": ("军工", "造船", "船舶"),
    "房地产开发": ("房地产", "楼市", "房价"),
    "煤炭": ("煤炭", "煤价"),
    "石油": ("石油", "油价", "原油"),
    "游戏": ("游戏", "版号"),
    "传媒": ("传媒", "影视"),
    "教育": ("教育",),
    "旅游酒店": ("旅游", "出行", "酒店"),
    "物流": ("物流", "快递"),
    "农牧饲渔": ("农业", "粮食", "生猪", "猪价"),
}

# 同花顺板块名 -> 基金简称搜索词（无映射的板块用板块名本身兜底）
SECTOR_FUND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "半导体": ("半导体", "芯片"),
    "软件开发": ("软件", "计算机"),
    "互联网服务": ("互联网", "数字经济"),
    "计算机设备": ("计算机", "信创"),
    "通信设备": ("通信", "5G"),
    "通信服务": ("通信",),
    "银行": ("银行",),
    "保险": ("保险",),
    "证券": ("证券", "券商"),
    "白酒": ("白酒", "食品饮料"),
    "食品加工": ("食品饮料", "消费"),
    "医疗": ("医疗", "创新药"),
    "医疗器械": ("医疗器械", "医疗"),
    "生物制品": ("生物医药", "创新药"),
    "化学制药": ("医药", "创新药"),
    "有色金属": ("有色",),
    "贵金属": ("黄金", "贵金属"),
    "小金属": ("有色金属", "稀土"),
    "能源金属": ("有色金属", "新能源"),
    "光伏设备": ("光伏",),
    "电池": ("电池", "新能源车"),
    "电网设备": ("电网", "电力"),
    "电力": ("电力",),
    "汽车整车": ("新能源汽车", "汽车"),
    "汽车服务": ("汽车",),
    "航天航空": ("军工", "航空航天"),
    "船舶制造": ("军工",),
    "房地产开发": ("房地产",),
    "煤炭": ("煤炭", "能源"),
    "石油": ("石油", "油气"),
    "游戏": ("游戏", "传媒"),
    "传媒": ("传媒",),
    "教育": ("教育",),
    "旅游酒店": ("旅游",),
    "物流": ("物流",),
    "农牧饲渔": ("农业",),
}

P = ParamSpec("P")
T = TypeVar("T")

# 东财行业名的罗马数字层级后缀，如 "银行Ⅱ"、"航天装备Ⅲ"
_INDUSTRY_SUFFIX_RE = re.compile(r"[ⅠⅡⅢⅣⅤ]+$")

# 基金简称尾部的份额类别标记，同一基金 A/C 份额去重用
_SHARE_CLASS_RE = re.compile(r"[ABC]$")


@dataclass(frozen=True)
class NewsItem:
    """单条财经快讯。"""

    标题: str
    摘要: str
    时间: str


@dataclass(frozen=True)
class NewsStat:
    """板块相关快讯的情绪统计。"""

    正面: int
    负面: int
    代表: list[NewsItem]


@dataclass(frozen=True)
class IndustryEarnings:
    """单板块在最新披露期的业绩统计。"""

    净利增速中位: float | None
    营收增速中位: float | None
    公司数: int
    龙头: str


@dataclass
class SectorAssessment:
    """单板块的三维评分与全部依据。"""

    板块: str
    净额20日: float
    净额当日: float | None
    涨跌幅20日: float
    新闻统计: NewsStat
    净利增速中位: float | None
    营收增速中位: float | None
    财报家数: int
    龙头: str
    评分: float
    依据: str
    定性: str


@dataclass(frozen=True)
class FundPick:
    """单只推荐基金。"""

    代码: str
    简称: str
    来源: str
    近1月: float
    近3月: float
    今年来: float


@dataclass(frozen=True)
class MarketData:
    """板块轮动分析的市场原始数据。"""

    flow: pd.DataFrame
    headlines: list[NewsItem]
    earnings_map: dict[str, IndustryEarnings]
    财报期: str
    披露家数: int


@dataclass(frozen=True)
class FundSources:
    """基金排行数据源。"""

    场外: pd.DataFrame
    ETF: pd.DataFrame


@dataclass(frozen=True)
class RotationResult:
    """板块轮动分析的完整结果。"""

    数据: MarketData
    关注列表: list[SectorAssessment]
    风险列表: list[SectorAssessment]
    基金推荐: dict[str, list[FundPick]]
    生成时间: str


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


def to_float(value: object, default: float = 0.0) -> float:
    """把 DataFrame 行取出的 object 值安全转为 float，NaN/缺失取默认值。

    Args:
        value: DataFrame 单元格值。
        default: 值缺失时的返回值。

    Returns:
        数值。
    """

    if value is None:
        return default
    if isinstance(value, float | int):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def to_optional_float(value: object) -> float | None:
    """把 DataFrame 行取出的 object 值安全转为 float，NaN/缺失转 None。

    Args:
        value: DataFrame 单元格值。

    Returns:
        数值或 None。
    """

    result = to_float(value, default=float("nan"))
    return None if math.isnan(result) else result


def parse_pct(value: object) -> float:
    """把 "25.27%" 之类百分比文本转为浮点数。

    Args:
        value: 原始百分比文本。

    Returns:
        数值；无法解析时返回 nan。
    """

    text = str(value).strip().rstrip("%")
    try:
        return float(text)
    except ValueError:
        return float("nan")


def recent_quarter_ends(today: date) -> list[str]:
    """生成从近到远的季度末日期列表（YYYYMMDD）。

    Args:
        today: 基准日期。

    Returns:
        早于 today 的季度末日期串列表，从最近到最远排序。
    """

    ends: set[date] = set()
    for month, day in [(3, 31), (6, 30), (9, 30), (12, 31)]:
        for year in (today.year, today.year - 1):
            quarter_end = date(year, month, day)
            if quarter_end < today:
                ends.add(quarter_end)
    return sorted((q.strftime("%Y%m%d") for q in ends), reverse=True)


@retry()
def fetch_flow_20d() -> pd.DataFrame:
    """获取近 20 日行业资金流（同花顺，金额单位：亿元）。

    Returns:
        行业资金流表，含行业/阶段涨跌幅/流入资金/净额等列。
    """

    return ak.stock_fund_flow_industry(symbol="20日排行")


@retry()
def fetch_flow_today() -> pd.DataFrame:
    """获取当日行业资金流（同花顺，金额单位：亿元）。

    Returns:
        行业资金流表，含行业/净额等列。
    """

    return ak.stock_fund_flow_industry(symbol="即时")


def recent_trading_days(today: date, days: int = 22) -> list[str]:
    """生成从今天往前推的最近 N 个交易日（跳过周末）。

    Args:
        today: 基准日期。
        days: 需要的交易日数量。

    Returns:
        日期字符串列表（YYYYMMDD），从近到远排序。
    """

    result: list[str] = []
    current = today
    while len(result) < days:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            result.append(current.strftime("%Y%m%d"))
    return result


def fetch_monthly_headlines() -> list[NewsItem]:
    """获取近 1 个月的新闻联播文字稿用于板块情绪分析。

    Returns:
        近 1 个月的新闻列表，按日期倒序。
    """

    today = datetime.now().astimezone().date()
    all_items: list[NewsItem] = []
    for date_str in recent_trading_days(today, NEWS_LOOKBACK_DAYS):
        try:
            frame = fetch_news_cctv_api(date=date_str)
            for row in frame.to_dict("records"):
                all_items.append(
                    NewsItem(
                        标题=str(row["title"]),
                        摘要=str(row["content"]),
                        时间=f"{date_str[4:6]}-{date_str[6:8]}",
                    )
                )
        except Exception as exc:
            print(
                f"[跳过] {date_str} 新闻获取失败: {type(exc).__name__}",
                file=sys.stderr,
            )
    return all_items


@retry()
def fetch_earnings_quarter(quarter: str) -> pd.DataFrame:
    """获取指定季度业绩报表（东方财富）。

    Args:
        quarter: 财报期，如 "20260630"。

    Returns:
        业绩报表，含股票代码/简称/净利润同比/所处行业等列。
    """

    return fetch_earnings_api(date=quarter)


def fetch_earnings(quarter: str | None) -> tuple[str, pd.DataFrame]:
    """拉取业绩报表，未指定季度时从最近季度末逐季前溯。

    财报披露存在时间窗（如中报 7-8 月陆续披露），最近季度数据量
    不足时说明披露刚开始，此时回退到上一已完整披露的季度。

    Args:
        quarter: 指定财报期（YYYYMMDD），None 时自动推断。

    Returns:
        (财报期, 业绩报表)。

    Raises:
        RuntimeError: 最近 4 个季度均无足量数据。
    """

    if quarter:
        return quarter, fetch_earnings_quarter(quarter)
    today = datetime.now().astimezone().date()
    for date_str in recent_quarter_ends(today)[:4]:
        frame = fetch_earnings_quarter(date_str)
        if len(frame) >= MIN_EARNINGS_ROWS:
            return date_str, frame
    raise RuntimeError("最近 4 个季度均无足量业绩报表数据")


@retry()
def fetch_fund_rank() -> pd.DataFrame:
    """获取开放式基金排行（东方财富，全市场 2 万余只）。

    Returns:
        基金排行表，含基金代码/简称/近1月/近3月/今年来等列。
    """

    return fetch_fund_rank_raw(symbol="全部")


@retry()
def fetch_etf_rank() -> pd.DataFrame:
    """获取场内交易基金排行（东方财富，含 ETF）。

    Returns:
        场内基金排行表，含基金代码/简称/近1月/近3月/今年来等列。
    """

    return fetch_etf_rank_raw()


def build_flow_table(flow_20d: pd.DataFrame, flow_today: pd.DataFrame) -> pd.DataFrame:
    """合并 20 日与当日行业资金流为数值化分析底表。

    Args:
        flow_20d: 同花顺 20 日排行行业资金流。
        flow_today: 同花顺即时行业资金流。

    Returns:
        含 行业/净额20日/流入20日/净额当日/涨跌幅20日 与两列资金分位的表。
    """

    base = (
        flow_20d[["行业", "阶段涨跌幅", "流入资金", "净额"]]
        .rename(
            columns={
                "阶段涨跌幅": "涨跌幅20日",
                "流入资金": "流入20日",
                "净额": "净额20日",
            }
        )
        .copy()
    )
    base["涨跌幅20日"] = base["涨跌幅20日"].map(parse_pct)
    today = flow_today[["行业", "净额"]].rename(columns={"净额": "净额当日"})
    merged = base.merge(today, on="行业", how="left")
    for column in ["净额20日", "流入20日", "净额当日"]:
        merged[column] = pd.to_numeric(merged[column], errors="coerce")

    # 资金分位（0-1）即该维度得分系数，打分时直接乘权重
    merged["资金分位"] = merged["净额20日"].rank(pct=True)
    merged["当日分位"] = merged["净额当日"].rank(pct=True)
    return merged


def match_sector(industry: str, sectors: list[str]) -> str | None:
    """东财行业名匹配同花顺板块名。

    东财行业名带 Ⅱ/Ⅲ 层级后缀，去后缀后先精确匹配再互相包含匹配，
    覆盖 "银行Ⅱ"→"银行"、"电力行业"→"电力" 这类命名差异。

    Args:
        industry: 东财行业名。
        sectors: 同花顺板块名列表。

    Returns:
        匹配到的板块名，无匹配返回 None。
    """

    stem = _INDUSTRY_SUFFIX_RE.sub("", industry)
    for sector in sectors:
        if sector == stem:
            return sector
    for sector in sectors:
        if stem in sector or sector in stem:
            return sector
    return None


def median_or_none(series: pd.Series) -> float | None:
    """取序列非空中位数，全空时返回 None。

    Args:
        series: 数值序列。

    Returns:
        中位数或 None。
    """

    values = series.dropna()
    return float(values.median()) if not values.empty else None


def build_leader_text(group: pd.DataFrame) -> str:
    """生成板块净利润龙头的业绩描述文本。

    Args:
        group: 板块内全部公司的业绩报表行。

    Returns:
        如 "宁德时代(300750) 净利同比 +42.0%"，无有效数据返回空串。
    """

    profit = pd.to_numeric(group["净利润-净利润"], errors="coerce")
    leaders = group.assign(_利润=profit).dropna(subset=["_利润"]).nlargest(1, "_利润")
    if leaders.empty:
        return ""
    row = leaders.iloc[0]
    growth = pd.to_numeric(pd.Series([row["净利润-同比增长"]]), errors="coerce").iloc[0]
    growth_text = f"{growth:+.1f}%" if pd.notna(growth) else "无同比数据"
    return f"{row['股票简称']}({row['股票代码']}) 净利同比 {growth_text}"


def build_earnings_map(
    earnings: pd.DataFrame, sectors: list[str]
) -> dict[str, IndustryEarnings]:
    """把业绩报表按东财行业聚合后映射到同花顺板块。

    多个东财行业可能映射到同一板块（如 医疗器械/医疗美容→医疗），
    映射后合并统计中位数与龙头。

    Args:
        earnings: 业绩报表。
        sectors: 同花顺板块名列表。

    Returns:
        板块名到行业业绩统计的字典。
    """

    mapping: dict[str, list[str]] = {}
    for industry in earnings["所处行业"].dropna().unique():
        sector = match_sector(str(industry), sectors)
        if sector is not None:
            mapping.setdefault(sector, []).append(str(industry))
    result: dict[str, IndustryEarnings] = {}
    for sector, industries in mapping.items():
        group = earnings.loc[earnings["所处行业"].isin(industries)]
        profit_growth = pd.to_numeric(group["净利润-同比增长"], errors="coerce")
        revenue_growth = pd.to_numeric(group["营业总收入-同比增长"], errors="coerce")
        result[sector] = IndustryEarnings(
            净利增速中位=median_or_none(profit_growth),
            营收增速中位=median_or_none(revenue_growth),
            公司数=len(group),
            龙头=build_leader_text(group),
        )
    return result


def classify_sector_news(sector: str, headlines: list[NewsItem]) -> NewsStat:
    """统计与板块相关的快讯正负面条数与代表新闻。

    Args:
        sector: 同花顺板块名。
        headlines: 当日快讯列表。

    Returns:
        新闻情绪统计。
    """

    keywords = SECTOR_NEWS_KEYWORDS.get(sector, (sector,))
    positive: list[NewsItem] = []
    negative: list[NewsItem] = []
    for item in headlines:
        text = f"{item.标题} {item.摘要}"
        if not any(keyword in text for keyword in keywords):
            continue
        if any(keyword in text for keyword in NEGATIVE_KEYWORDS):
            negative.append(item)
        elif any(keyword in text for keyword in POSITIVE_KEYWORDS):
            positive.append(item)
    return NewsStat(
        正面=len(positive),
        负面=len(negative),
        代表=positive[:1] + negative[:1],
    )


def pick_headlines(headlines: list[NewsItem]) -> list[NewsItem]:
    """挑选报告头条区的重要快讯。

    优先取标题含宏观政策关键词的快讯，不足 HEADLINE_LIMIT 条时
    按时间顺序补齐，保证头条区不空。

    Args:
        headlines: 当日快讯列表（按时间倒序）。

    Returns:
        最多 HEADLINE_LIMIT 条重要快讯。
    """

    important = [
        item
        for item in headlines
        if any(keyword in item.标题 for keyword in HEADLINE_KEYWORDS)
    ]
    if len(important) >= HEADLINE_LIMIT:
        return important[:HEADLINE_LIMIT]
    rest = [item for item in headlines if item not in important]
    return (important + rest)[:HEADLINE_LIMIT]


def earnings_score(growth: float | None) -> float:
    """把财报净利增速中位数映射为基本面得分。

    Args:
        growth: 行业净利同比增速中位数（%），None 表示无财报数据。

    Returns:
        0-30 的得分，无数据按中性 15 分。
    """

    if growth is None:
        return EARNINGS_ZERO_SCORE
    if growth >= EARNINGS_FULL_GROWTH:
        return WEIGHT_EARNINGS
    # -50%→0 分、0%→15 分、+50%→30 分的线性插值，超界截断
    score = EARNINGS_ZERO_SCORE + growth / EARNINGS_FULL_GROWTH * (
        WEIGHT_EARNINGS - EARNINGS_ZERO_SCORE
    )
    return max(0.0, min(WEIGHT_EARNINGS, score))


def score_sector(
    flow_20d: float,
    flow_today: float | None,
    news: NewsStat,
    earnings: IndustryEarnings | None,
) -> tuple[float, str]:
    """对单板块做资金 / 情绪 / 基本面三维打分。

    Args:
        flow_20d: 20 日资金分位（0-1）。
        flow_today: 当日资金分位（0-1），None 表示无当日数据。
        news: 板块新闻情绪统计。
        earnings: 板块财报统计，None 表示无匹配财报。

    Returns:
        (总分, 依据描述)。
    """

    flow_score = flow_20d * WEIGHT_FLOW_20D
    # 当日无数据的板块按该维度一半得分，避免误判为极端强弱
    today_ratio = flow_today if flow_today is not None else 0.5
    today_score = today_ratio * WEIGHT_FLOW_TODAY
    net_news = news.正面 - news.负面
    news_score = max(
        0.0, min(WEIGHT_NEWS, NEWS_BASE_SCORE + NEWS_STEP_SCORE * net_news)
    )
    growth = earnings.净利增速中位 if earnings is not None else None
    fundamental_score = earnings_score(growth)
    total = flow_score + today_score + news_score + fundamental_score
    basis = (
        f"资金 {flow_score:.0f}/{WEIGHT_FLOW_20D:.0f}"
        f" + 当日 {today_score:.0f}/{WEIGHT_FLOW_TODAY:.0f}"
        f" + 情绪 {news_score:.0f}/{WEIGHT_NEWS:.0f}"
        f" + 财报 {fundamental_score:.0f}/{WEIGHT_EARNINGS:.0f}"
    )
    return round(total, 1), basis


def qualify(score: float) -> str:
    """按总分给出板块定性。

    Args:
        score: 三维总分。

    Returns:
        重点关注 / 中性观望 / 建议回避。
    """

    if score >= WATCH_THRESHOLD:
        return "重点关注"
    if score >= NEUTRAL_THRESHOLD:
        return "中性观望"
    return "建议回避"


def assess_all(data: MarketData) -> list[SectorAssessment]:
    """对全部行业板块逐个打分。

    Args:
        data: 市场原始数据。

    Returns:
        全部板块的评估列表。
    """

    results: list[SectorAssessment] = []
    for row in data.flow.to_dict("records"):
        sector = str(row["行业"])
        news = classify_sector_news(sector, data.headlines)
        earnings = data.earnings_map.get(sector)
        flow_today_raw = row["净额当日"]
        today_rank = row["当日分位"]
        score, basis = score_sector(
            flow_20d=to_float(row["资金分位"]),
            flow_today=to_float(today_rank),
            news=news,
            earnings=earnings,
        )
        results.append(
            SectorAssessment(
                板块=sector,
                净额20日=to_float(row["净额20日"]),
                净额当日=to_float(flow_today_raw),
                涨跌幅20日=to_float(row["涨跌幅20日"]),
                新闻统计=news,
                净利增速中位=earnings.净利增速中位 if earnings else None,
                营收增速中位=earnings.营收增速中位 if earnings else None,
                财报家数=earnings.公司数 if earnings else 0,
                龙头=earnings.龙头 if earnings else "",
                评分=score,
                依据=basis,
                定性=qualify(score),
            )
        )
    return results


def dedupe_share_class(picks: list[FundPick]) -> list[FundPick]:
    """同一基金的多份额（A/C）只保留排序最前的一只。

    Args:
        picks: 排序后的推荐候选。

    Returns:
        去重后的候选列表。
    """

    seen: set[str] = set()
    result: list[FundPick] = []
    for pick in picks:
        base = _SHARE_CLASS_RE.sub("", pick.简称)
        if base in seen:
            continue
        seen.add(base)
        result.append(pick)
    return result


def rank_fund_frame(
    frame: pd.DataFrame, keywords: tuple[str, ...], source: str
) -> list[FundPick]:
    """按关键词与近端动量从基金排行中筛选推荐候选。

    优先保留近 3 月为正的品种，避免推荐仍处下行趋势的主题基金；
    动量分 = 近3月 + 0.5×近1月。

    Args:
        frame: 基金排行表（场外或 ETF）。
        keywords: 简称匹配关键词。
        source: 来源标签（"场外"/"ETF"）。

    Returns:
        匹配候选列表，按动量降序（去重与总量限制由调用方处理）。
    """

    pattern = "|".join(keywords)
    candidates = frame.loc[frame["基金简称"].str.contains(pattern, na=False)]
    candidates = candidates.dropna(subset=["近1月", "近3月"])
    if candidates.empty:
        return []
    uptrend = candidates.loc[candidates["近3月"].astype(float) > 0]
    if not uptrend.empty:
        candidates = uptrend
    momentum = candidates["近3月"].astype(float) + 0.5 * candidates["近1月"].astype(
        float
    )
    ranked = candidates.assign(_动量=momentum).sort_values("_动量", ascending=False)
    return [
        FundPick(
            代码=str(item["基金代码"]),
            简称=str(item["基金简称"]),
            来源=source,
            近1月=float(item["近1月"]),
            近3月=float(item["近3月"]),
            今年来=float(item["今年来"]) if pd.notna(item["今年来"]) else float("nan"),
        )
        for item in ranked.to_dict("records")
    ]


def build_recommendations(
    watch: list[SectorAssessment], sources: FundSources
) -> dict[str, list[FundPick]]:
    """从全部关注板块中挑选动量最强的基金/ETF 推荐。

    Args:
        watch: 值得关注板块列表。
        sources: 基金排行数据源。

    Returns:
        板块名到推荐基金列表的字典，合计不超过 TOTAL_FUND_PICKS 只。
    """

    all_picks: list[tuple[FundPick, str]] = []
    for item in watch:
        keywords = SECTOR_FUND_KEYWORDS.get(item.板块, (item.板块,))
        for pick in rank_fund_frame(sources.场外, keywords, "场外"):
            all_picks.append((pick, item.板块))
        for pick in rank_fund_frame(sources.ETF, keywords, "ETF"):
            all_picks.append((pick, item.板块))
    all_picks.sort(key=lambda x: x[0].近3月 + 0.5 * x[0].近1月, reverse=True)
    seen: set[str] = set()
    top: list[tuple[FundPick, str]] = []
    for pick, sector in all_picks:
        base = _SHARE_CLASS_RE.sub("", pick.简称)
        if base in seen:
            continue
        seen.add(base)
        top.append((pick, sector))
        if len(top) >= TOTAL_FUND_PICKS:
            break
    result: dict[str, list[FundPick]] = {}
    for pick, sector in top:
        result.setdefault(sector, []).append(pick)
    return result


def flow_text(amount: float | None) -> str:
    """资金净额转为带方向的文本。

    Args:
        amount: 净额（亿元），None 表示无数据。

    Returns:
        如 "净流入 +120.4 亿"。
    """

    if amount is None:
        return "无数据"
    if amount >= 0:
        return f"净流入 {amount:+.1f} 亿"
    return f"净流出 {amount:.1f} 亿"


def growth_text(value: float | None) -> str:
    """增速值转为文本，None 或 nan 显示为无数据。

    Args:
        value: 增速（%）。

    Returns:
        如 "+42.0%"。
    """

    if value is None or pd.isna(value):
        return "无数据"
    return f"{value:+.1f}%"


def render_sector(
    assessment: SectorAssessment, picks: list[FundPick], prefix: str
) -> str:
    """渲染单板块的评估详情文本。

    Args:
        assessment: 板块评估结果。
        picks: 该板块的推荐基金（风险板块传空）。
        prefix: 序号前缀。

    Returns:
        板块详情文本。
    """

    earnings = assessment.财报家数
    if earnings > 0:
        earnings_line = (
            f"    财报: 披露 {earnings} 家, 净利同比中位 {growth_text(assessment.净利增速中位)}"
            f", 营收中位 {growth_text(assessment.营收增速中位)}"
        )
        if assessment.龙头:
            earnings_line += f", 龙头 {assessment.龙头}"
    else:
        earnings_line = "    财报: 无匹配行业数据（按中性计分）"
    lines = [
        f"--- {prefix}{assessment.板块}（评分 {assessment.评分:.0f}，{assessment.定性}）",
        (
            f"    近20日: {flow_text(assessment.净额20日)}"
            f" | 当日: {flow_text(assessment.净额当日)}"
            f" | 区间涨跌 {growth_text(assessment.涨跌幅20日)}"
        ),
        f"    情绪: 快讯正面 {assessment.新闻统计.正面} / 负面 {assessment.新闻统计.负面}",
        earnings_line,
        f"    依据: {assessment.依据}",
    ]
    for item in assessment.新闻统计.代表:
        lines.append(f"    代表新闻: [{item.时间}] {item.标题}")
    for pick in picks:
        lines.append(
            f"    推荐[{pick.来源}] {pick.简称}({pick.代码})"
            f" 近1月 {growth_text(pick.近1月)}"
            f" 近3月 {growth_text(pick.近3月)}"
            f" 今年来 {growth_text(pick.今年来)}"
        )
    return "\n".join(lines)


def render_report(result: RotationResult) -> str:
    """渲染完整板块轮动分析报告文本。

    Args:
        result: 分析结果聚合对象。

    Returns:
        报告文本。
    """

    parts: list[str] = [
        "=" * 70,
        f"【板块轮动分析报告】生成时间 {result.生成时间}",
        (
            f"财报期 {result.数据.财报期}（已披露 {result.数据.披露家数} 家）"
            f" | 行业数 {len(result.数据.flow)} | 新闻 {len(result.数据.headlines)} 条（近1月）"
        ),
        "评分构成: 20日资金 40 + 当日资金 10 + 新闻情绪 20 + 财报基本面 30",
        "",
        "=" * 70,
        "【一、近期重要新闻】",
    ]
    for item in pick_headlines(result.数据.headlines):
        parts.append(f"  [{item.时间}] {item.标题}")

    parts += ["", "=" * 70, "【二、未来值得关注的板块】"]
    for index, assessment in enumerate(result.关注列表, start=1):
        picks = result.基金推荐.get(assessment.板块, [])
        parts.append(render_sector(assessment, picks, f"{index}. "))
        parts.append("")

    parts += ["=" * 70, "【三、建议回避的板块】"]
    for index, assessment in enumerate(result.风险列表, start=1):
        parts.append(render_sector(assessment, [], f"{index}. "))
        parts.append("")

    parts += [
        "=" * 70,
        "【四、免责声明】",
        "  本报告由公开数据与固定规则自动生成，评分与推荐不构成投资建议。",
        "  快讯仅覆盖当日窗口；财报为最新披露期（可能尚未披露完毕）；",
        "  基金按近期动量筛选，历史表现不代表未来收益。市场有风险，投资需谨慎。",
    ]
    return "\n".join(parts)


def json_num(value: float | None) -> float | None:
    """NaN 转 None，保证输出合法 JSON。

    Args:
        value: 可能为 nan 的数值。

    Returns:
        合法 JSON 数值或 None。
    """

    if value is None or pd.isna(value):
        return None
    return float(value)


def to_json(result: RotationResult) -> dict[str, object]:
    """将分析结果序列化为可 JSON 序列化的字典。

    Args:
        result: 分析结果聚合对象。

    Returns:
        结构化分析结果。
    """

    def sector_json(assessment: SectorAssessment) -> dict[str, object]:
        return {
            "板块": assessment.板块,
            "评分": assessment.评分,
            "定性": assessment.定性,
            "依据": assessment.依据,
            "近20日净额亿": json_num(assessment.净额20日),
            "当日净额亿": json_num(assessment.净额当日),
            "区间涨跌幅": json_num(assessment.涨跌幅20日),
            "新闻正面": assessment.新闻统计.正面,
            "新闻负面": assessment.新闻统计.负面,
            "净利增速中位": json_num(assessment.净利增速中位),
            "营收增速中位": json_num(assessment.营收增速中位),
            "财报家数": assessment.财报家数,
            "龙头": assessment.龙头,
        }

    def pick_json(pick: FundPick) -> dict[str, object]:
        return {
            "代码": pick.代码,
            "简称": pick.简称,
            "来源": pick.来源,
            "近1月": json_num(pick.近1月),
            "近3月": json_num(pick.近3月),
            "今年来": json_num(pick.今年来),
        }

    watch = [
        dict(
            sector_json(assessment),
            推荐基金=[
                pick_json(pick) for pick in result.基金推荐.get(assessment.板块, [])
            ],
        )
        for assessment in result.关注列表
    ]
    return {
        "生成时间": result.生成时间,
        "财报期": result.数据.财报期,
        "披露家数": result.数据.披露家数,
        "关注板块": watch,
        "回避板块": [sector_json(item) for item in result.风险列表],
    }


def collect_market_data(quarter: str | None) -> MarketData:
    """抓取资金流、快讯与财报数据并组装分析底表。

    Args:
        quarter: 指定财报期（YYYYMMDD），None 时自动推断。

    Returns:
        市场原始数据。
    """

    flow = build_flow_table(fetch_flow_20d(), fetch_flow_today())
    headlines = fetch_monthly_headlines()
    财报期, earnings = fetch_earnings(quarter)
    earnings_map = build_earnings_map(earnings, flow["行业"].tolist())
    return MarketData(
        flow=flow,
        headlines=headlines,
        earnings_map=earnings_map,
        财报期=财报期,
        披露家数=len(earnings),
    )


def main() -> None:
    """命令行入口，抓取数据并输出板块轮动分析报告。"""

    # Windows 终端默认 GBK 编码，重配为标准 UTF-8 保证中文正常显示
    # 先落局部变量再收窄，避免对 sys 模块成员收窄引入 Unknown 类型参数
    stdout = sys.stdout
    stderr = sys.stderr
    if isinstance(stdout, io.TextIOWrapper):
        stdout.reconfigure(encoding="utf-8")
    if isinstance(stderr, io.TextIOWrapper):
        stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="板块轮动分析与基金推荐")
    parser.add_argument(
        "--top-n", type=int, default=10, help="值得关注板块数量（默认 10）"
    )
    parser.add_argument(
        "--bottom-n", type=int, default=5, help="建议回避板块数量（默认 5）"
    )
    parser.add_argument(
        "--quarter",
        default=None,
        help="指定财报期 YYYYMMDD（默认自动推断最近披露期）",
    )
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON 结果")
    args = parser.parse_args()

    try:
        data = collect_market_data(args.quarter)
        sources = FundSources(场外=fetch_fund_rank(), ETF=fetch_etf_rank())
        assessments = assess_all(data)
        ordered = sorted(assessments, key=lambda item: item.评分, reverse=True)
        watch = ordered[: args.top_n]
        risk = sorted(assessments, key=lambda item: item.评分)[: args.bottom_n]
        picks = build_recommendations(watch, sources)
        result = RotationResult(
            数据=data,
            关注列表=watch,
            风险列表=risk,
            基金推荐=picks,
            生成时间=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
        )
        if args.json:
            print(json.dumps(to_json(result), ensure_ascii=False, indent=2))
        else:
            print(render_report(result))
    except Exception as exc:
        # CLI 入口统一兜底：数据源异常转为友好消息
        print(f"板块轮动分析失败: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
