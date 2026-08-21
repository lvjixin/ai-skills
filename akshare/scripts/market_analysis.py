"""A股市场资金流向分析脚本。

汇总沪深指数行情、大盘资金流向（东方财富）、行业/概念/个股资金流（同花顺），
输出当日市场概览与净流入/净流出 TOP 榜单，用于快速把握市场资金动向。

用法示例:
    python market_analysis.py
"""

import functools
import io
import sys
import time
from collections.abc import Callable
from typing import ParamSpec, TypeVar

import akshare as ak
import pandas as pd
import requests

# 终端表格显示配置：完整显示所有列，中文按等宽对齐
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)
pd.set_option("display.unicode.east_asian_width", True)

# 重点跟踪的指数代码（新浪代码）与中文名，字典顺序即报告展示顺序
KEY_INDEXES: dict[str, str] = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000688": "科创50",
    "sh000300": "沪深300",
    "sh000905": "中证500",
    "sh000852": "中证1000",
    "sh000016": "上证50",
}

# 大盘资金流日线表中非金额列的列名
DATE_COLUMN = "日期"

P = ParamSpec("P")
T = TypeVar("T")


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


@retry()
def fetch_index_spot() -> pd.DataFrame:
    """获取沪深全量指数实时行情（新浪）。

    Returns:
        全量指数行情表，含代码/名称/最新价/涨跌幅/成交额等列。
    """

    return ak.stock_zh_index_spot_sina()


@retry()
def fetch_industry_flow() -> pd.DataFrame:
    """获取行业资金流（同花顺，金额单位：亿元）。

    Returns:
        行业资金流表。
    """

    return ak.stock_fund_flow_industry(symbol="即时")


@retry()
def fetch_concept_flow() -> pd.DataFrame:
    """获取概念资金流（同花顺，金额单位：亿元）。

    Returns:
        概念资金流表。
    """

    return ak.stock_fund_flow_concept(symbol="即时")


@retry()
def fetch_individual_flow() -> pd.DataFrame:
    """获取个股资金流（同花顺，金额为带单位的字符串）。

    Returns:
        个股资金流表。
    """

    return ak.stock_fund_flow_individual(symbol="即时")


@retry()
def fetch_market_fund_flow_direct() -> pd.DataFrame:
    """获取大盘资金流向历史日线（东方财富直连接口）。

    AKShare 未提供大盘资金流接口，直连东方财富 push2his 补齐；
    返回的 klines 是逗号分隔的字符串数组，拆分后最后一行为最新交易日。

    Returns:
        大盘资金流日线表，字段含日期/主力净额/上证收盘等。
    """

    headers: dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Referer": "https://data.eastmoney.com/",
    }
    params: dict[str, str] = {
        "lmt": "0",
        "klt": "101",
        "secid": "1.000001",
        "secid2": "0.399001",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    with requests.get(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        params=params,
        headers=headers,
        timeout=15,
    ) as response:
        response.raise_for_status()
        klines: list[str] = response.json()["data"]["klines"]
    columns: list[str] = [
        "日期",
        "主力净额",
        "小单净额",
        "中单净额",
        "大单净额",
        "超大单净额",
        "主力占比",
        "小单占比",
        "中单占比",
        "大单占比",
        "超大单占比",
        "上证收盘",
        "上证涨跌幅",
        "深证收盘",
        "深证涨跌幅",
    ]
    return pd.DataFrame([kline.split(",") for kline in klines], columns=columns)


def select_key_indexes(spot: pd.DataFrame) -> pd.DataFrame:
    """从全量指数行情中筛选重点指数并按配置顺序排列。

    Args:
        spot: 新浪全量指数行情表。

    Returns:
        重点指数行情表，列含代码/名称/最新价/涨跌幅/成交额/最高/最低/今开。
    """

    frame = spot.copy()
    frame["代码"] = frame["代码"].astype(str).str.lower()
    mask = frame["代码"].isin(KEY_INDEXES)
    columns = ["代码", "名称", "最新价", "涨跌幅", "成交额", "最高", "最低", "今开"]
    indexes: pd.DataFrame = frame.loc[mask, columns].copy()
    # 按 KEY_INDEXES 顺序重排；未匹配到的指数会置为 NaN，剔除后保证展示顺序稳定
    indexes = (
        indexes.set_index("代码")
        .reindex(list(KEY_INDEXES))
        .dropna(subset=["名称"])
        .reset_index()
    )
    return indexes


def parse_amount_yi(value: object) -> float:
    """把同花顺金额字符串转换为以亿元为单位的数值。

    同花顺个股资金流以字符串返回金额，可能带"亿"或"万"后缀；
    无后缀时按原值（元）换算。

    Args:
        value: 原始金额。

    Returns:
        以亿元为单位的数值。
    """

    text = str(value)
    if text.endswith("亿"):
        return float(text[:-1])
    if text.endswith("万"):
        return float(text[:-1]) / 1e4
    try:
        return float(text) / 1e8
    except ValueError:
        return float("nan")


def rank_flows(
    frame: pd.DataFrame, sort_key: str, top_n: int = 10, bottom_n: int = 5
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按指定金额列排序，返回净流入与净流出两个榜单。

    Args:
        frame: 数值化后的资金流表。
        sort_key: 用于排序的金额列名。
        top_n: 净流入榜单行数。
        bottom_n: 净流出榜单行数。

    Returns:
        净流入榜与净流出榜。
    """

    ranked = frame.sort_values(sort_key, ascending=False)
    return ranked.head(top_n), ranked.tail(bottom_n)


def render_index_report(spot: pd.DataFrame) -> str:
    """渲染重点指数行情报告文本。

    Args:
        spot: 新浪全量指数行情表。

    Returns:
        重点指数表格与沪深两市合计成交额的文本。
    """

    indexes = select_key_indexes(spot)
    indexes["成交额(亿)"] = indexes["成交额"].astype(float) / 1e8
    table = indexes.drop(columns=["成交额"]).to_string(
        index=False, float_format=lambda value: f"{value:.2f}"
    )
    sh_amount = float(indexes.loc[indexes["代码"] == "sh000001", "成交额"].iloc[0])
    sz_amount = float(indexes.loc[indexes["代码"] == "sz399001", "成交额"].iloc[0])
    total = f"沪深两市合计成交额约: {(sh_amount + sz_amount) / 1e8:,.0f} 亿元"
    return f"{table}\n\n{total}"


def render_market_flow_report(flow: pd.DataFrame) -> str:
    """渲染大盘资金流向报告文本（东方财富）。

    Args:
        flow: 大盘资金流日线表。

    Returns:
        最新交易日的指数收盘与分档资金净流入的文本。
    """

    numeric = flow.copy()
    for column in numeric.columns:
        if column != DATE_COLUMN:
            numeric[column] = pd.to_numeric(numeric[column], errors="coerce")
    latest = numeric.iloc[-1]
    lines: list[str] = [
        f"日期: {latest['日期']}",
        f"上证: 收盘 {latest['上证收盘']:.2f} ({latest['上证涨跌幅']:+.2f}%)   深证: {latest['深证收盘']:.2f} ({latest['深证涨跌幅']:+.2f}%)",
    ]
    for label, key in [
        ("主力净流入", "主力"),
        ("超大单", "超大单"),
        ("大单", "大单"),
        ("中单", "中单"),
        ("小单", "小单"),
    ]:
        amount = float(latest[f"{key}净额"]) / 1e8
        ratio = float(latest[f"{key}占比"])
        lines.append(f"{label}: {amount:+,.1f} 亿元 (净占比 {ratio:+.2f}%)")
    return "\n".join(lines)


def render_industry_report(frame: pd.DataFrame) -> str:
    """渲染行业资金流报告文本（同花顺，单位：亿元）。

    含净流入 TOP10、净流出 TOP5 与全行业汇总。

    Args:
        frame: 行业资金流表。

    Returns:
        报告文本。
    """

    numeric = frame.copy()
    for column in ["流入资金", "流出资金", "净额"]:
        numeric[column] = pd.to_numeric(numeric[column], errors="coerce")
    top, bottom = rank_flows(numeric, "净额")
    top_body = top[
        [
            "行业",
            "行业-涨跌幅",
            "流入资金",
            "流出资金",
            "净额",
            "领涨股",
            "领涨股-涨跌幅",
        ]
    ].to_string(index=False)
    bottom_body = bottom[
        ["行业", "行业-涨跌幅", "流入资金", "流出资金", "净额"]
    ].to_string(index=False)
    stats = f"全行业净额合计: {numeric['净额'].sum():+,.1f} 亿元；净流入行业数 {(numeric['净额'] > 0).sum()} / {len(numeric)}"
    return (
        "【行业资金流-今日 净流入 TOP10（同花顺，亿元）】\n"
        f"{top_body}\n\n"
        "【行业资金流-今日 净流出 TOP5（亿元）】\n\n"
        f"{bottom_body}\n\n"
        f"{stats}"
    )


def render_concept_report(frame: pd.DataFrame) -> str:
    """渲染概念资金流报告文本（同花顺，单位：亿元）。

    含净流入 TOP10 与净流出 TOP5。

    Args:
        frame: 概念资金流表。

    Returns:
        报告文本。
    """

    numeric = frame.copy()
    for column in ["流入资金", "流出资金", "净额"]:
        numeric[column] = pd.to_numeric(numeric[column], errors="coerce")
    top, bottom = rank_flows(numeric, "净额")
    top_body = top[
        ["行业", "行业-涨跌幅", "净额", "领涨股", "领涨股-涨跌幅"]
    ].to_string(index=False)
    bottom_body = bottom[["行业", "行业-涨跌幅", "净额"]].to_string(index=False)
    return (
        "【概念资金流-今日 净流入 TOP10（同花顺，亿元）】\n"
        f"{top_body}\n\n"
        "【概念资金流-今日 净流出 TOP5（亿元）】\n\n"
        f"{bottom_body}"
    )


def render_individual_report(frame: pd.DataFrame) -> str:
    """渲染个股资金流报告文本（同花顺）。

    个股金额列为带单位的字符串，先统一换算为亿元单位。
    含净流入 TOP10 与净流出 TOP5。

    Args:
        frame: 个股资金流表。

    Returns:
        报告文本。
    """

    numeric = frame.copy()
    for column in ["流入资金", "流出资金", "净额", "成交额"]:
        numeric[column + "(亿)"] = numeric[column].map(parse_amount_yi)
    top, bottom = rank_flows(numeric, "净额(亿)")
    columns = ["股票代码", "股票简称", "最新价", "涨跌幅", "净额(亿)", "成交额(亿)"]
    top_body = top[columns].to_string(index=False)
    bottom_body = bottom[columns].to_string(index=False)
    return (
        "【个股资金流-今日 净流入 TOP10（同花顺，亿元）】\n"
        f"{top_body}\n\n"
        "【个股资金流-今日 净流出 TOP5（亿元）】\n\n"
        f"{bottom_body}"
    )


def render_report() -> str:
    """抓取全部行情数据并渲染完整资金流报告。

    Returns:
        完整报告文本，各板块以分隔线间隔。
    """

    spot = fetch_index_spot()
    flow = fetch_market_fund_flow_direct()
    industry = fetch_industry_flow()
    concept = fetch_concept_flow()
    individual = fetch_individual_flow()

    parts: list[str] = [
        "=" * 70,
        "【沪深重要指数】",
        render_index_report(spot),
        "",
        "=" * 70,
        "【大盘资金流向（东方财富）】",
        render_market_flow_report(flow),
        "",
        "=" * 70,
        "【行业资金流（同花顺）】",
        render_industry_report(industry),
        "",
        "=" * 70,
        "【概念资金流（同花顺）】",
        render_concept_report(concept),
        "",
        "=" * 70,
        "【个股资金流（同花顺）】",
        render_individual_report(individual),
    ]
    return "\n".join(parts)


def main() -> None:
    """命令行入口，抓取数据并打印完整资金流报告。"""

    # Windows 终端默认 GBK 编码，重配为标准 UTF-8 保证中文正常显示
    # 先落局部变量再收窄，避免对 sys 模块成员收窄引入 Unknown 类型参数
    stdout = sys.stdout
    stderr = sys.stderr
    if isinstance(stdout, io.TextIOWrapper):
        stdout.reconfigure(encoding="utf-8")
    if isinstance(stderr, io.TextIOWrapper):
        stderr.reconfigure(encoding="utf-8")

    try:
        print(render_report())
    except Exception as exc:
        # CLI 入口统一兜底：数据源异常转为友好消息
        print(f"资金流分析失败: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
