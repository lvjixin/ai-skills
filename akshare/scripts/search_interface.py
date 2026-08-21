"""AKShare 接口检索脚本。

基于 AKShare 内置的离线检索层（ak.search / ak.list_categories / ak.interface_info）
按关键词或类目定位接口，或查询单个接口的完整元数据（参数、输出字段、调用示例）。

用法示例:
    python search_interface.py "可转债 实时行情"
    python search_interface.py "历史行情" --category stock --limit 10
    python search_interface.py --categories
    python search_interface.py --info stock_zh_a_hist --json
"""

import argparse
import io
import json
import sys
from dataclasses import dataclass
from typing import Any

import akshare as ak
import pandas as pd


@dataclass(frozen=True)
class SearchOptions:
    """关键词检索的过滤条件。"""

    limit: int = 20
    category: str | None = None
    documented_only: bool = False


def search_interfaces(query: str, options: SearchOptions) -> pd.DataFrame:
    """按关键词检索接口，按匹配度降序返回。

    结果列: 接口名 / 类目 / 描述 / 有无文档 / 匹配分。

    Args:
        query: 关键词，支持空格或逗号分隔；传入完整接口名时该接口置顶。
        options: 检索过滤条件。

    Returns:
        检索结果表。
    """

    return ak.search(
        query=query,
        limit=options.limit,
        category=options.category,
        documented_only=options.documented_only,
    )


def fetch_categories() -> pd.DataFrame:
    """列出全部类目及其接口数量，用于确定 category 可选值。

    Returns:
        类目与接口数两列的表。
    """

    return ak.list_categories()


def fetch_interface_info(name: str) -> dict[str, Any]:
    """查询单个接口的完整元数据。

    接口不存在时 AKShare 抛出异常，异常消息中附有最接近的候选名。

    Args:
        name: 接口名。

    Returns:
        含 name / module / category / desc / url / params / outputs / example 等键的字典。
    """

    return ak.interface_info(name)


def frame_to_json(frame: pd.DataFrame) -> str:
    """把检索结果表转换为紧凑的 JSON 字符串。

    Args:
        frame: 待转换的表。

    Returns:
        JSON 文本，每条记录一行。
    """

    return json.dumps(frame.to_dict(orient="records"), ensure_ascii=False)


def render_text(frame: pd.DataFrame) -> str:
    """把检索结果表渲染为人类可读的表格文本。

    Args:
        frame: 待渲染的表。

    Returns:
        表格文本。
    """

    return frame.to_string(index=False)


def render_info_text(info: dict[str, Any]) -> str:
    """把单接口元数据渲染为人类可读的文本。

    Args:
        info: interface_info 返回的元数据字典。

    Returns:
        格式化文本。
    """

    lines = [
        f"接口名:   {info.get('name')}",
        f"类目:     {info.get('category')}",
        f"模块:     {info.get('module')}",
        f"描述:     {info.get('desc')}",
        f"数据源:   {info.get('url')}",
        f"数据量:   {info.get('limit_desc')}",
        f"有无文档: {info.get('documented')}",
    ]

    # 元数据里 params/outputs 是字典列表，显式注解避免 or [] 推断出 Unknown
    params: list[dict[str, Any]] = info.get("params") or []
    if params:
        lines.append("\n输入参数:")
        for param in params:
            lines.append(
                f"  - {param.get('name')} ({param.get('type')}): {param.get('desc')}"
            )
    else:
        lines.append("\n输入参数: 无")

    outputs: list[dict[str, Any]] = info.get("outputs") or []
    if outputs:
        lines.append("\n输出字段:")
        for field in outputs:
            lines.append(
                f"  - {field.get('name')} ({field.get('type')}): {field.get('desc')}"
            )

    example = info.get("example")
    if example:
        lines.append("\n调用示例:")
        lines.append(example)

    return "\n".join(lines)


def main() -> None:
    """命令行入口，负责解析参数并分发到三种查询模式。"""

    parser = argparse.ArgumentParser(description="AKShare 接口检索")
    parser.add_argument("query", nargs="?", help="检索关键词，可留空")
    parser.add_argument("--limit", type=int, default=20, help="返回条数上限，默认 20")
    parser.add_argument("--category", help="限定类目，可用 --categories 查看可选值")
    parser.add_argument(
        "--documented-only", action="store_true", help="仅返回本文档已收录的接口"
    )
    parser.add_argument("--categories", action="store_true", help="列出全部类目")
    parser.add_argument("--info", help="查询单个接口的完整元数据")
    parser.add_argument(
        "--json", action="store_true", dest="json_output", help="输出 JSON 格式"
    )
    args = parser.parse_args()

    # Windows 终端默认 GBK 编码，重配为标准 UTF-8 保证中文正常显示
    # 先落局部变量再收窄，避免对 sys 模块成员收窄引入 Unknown 类型参数
    stdout = sys.stdout
    stderr = sys.stderr
    if isinstance(stdout, io.TextIOWrapper):
        stdout.reconfigure(encoding="utf-8")
    if isinstance(stderr, io.TextIOWrapper):
        stderr.reconfigure(encoding="utf-8")

    try:
        if args.categories:
            frame = fetch_categories()
            print(frame_to_json(frame) if args.json_output else render_text(frame))
        elif args.info:
            info = fetch_interface_info(args.info)
            if args.json_output:
                print(json.dumps(info, ensure_ascii=False, indent=2))
            else:
                print(render_info_text(info))
        elif args.query:
            options = SearchOptions(
                limit=args.limit,
                category=args.category,
                documented_only=args.documented_only,
            )
            frame = search_interfaces(args.query, options)
            print(frame_to_json(frame) if args.json_output else render_text(frame))
        else:
            parser.error("请提供检索关键词、--categories 或 --info")

    # CLI 入口统一兜底：akshare 异常（接口校验、网络等）直接转为友好消息
    except Exception as exc:
        print(f"检索失败: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
