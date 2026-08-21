"""AKShare 接口执行脚本。

对 AKShare 接口进行动态调用：接口名与参数由命令行传入，
脚本通过 getattr(ak, 接口名) 取得函数对象后用 **kwargs 方式调用，
结果按需输出为 CSV / JSON / 预览文本。

典型流程: 先用 search_interface.py 定位接口，再用本脚本执行。

用法示例:
    python call_interface.py stock_zh_a_hist
    python call_interface.py stock_zh_a_hist --params '{"start_date": "20250101", "end_date": "20250201"}'
    python call_interface.py stock_zh_a_hist --params '{"start_date": "20250101"}' --format csv --output out.csv
    python call_interface.py interface_info --params '{"name": "stock_zh_a_hist"}'
"""

import argparse
import io
import json
import sys
from collections.abc import Callable
from typing import Any, cast

import akshare as ak
import pandas as pd

# 预览模式下最多展示的行数，避免刷屏
PREVIEW_ROWS = 20


def parse_params(raw: str) -> dict[str, Any]:
    """解析命令行传入的 JSON 参数串。

    Args:
        raw: JSON 文本，例如 '{"start_date": "20250101"}'。

    Returns:
        参数字典。

    Raises:
        TypeError: 解析结果不是 JSON 对象。
    """

    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise TypeError("--params 必须是 JSON 对象")

    # isinstance 只能收窄到 dict[Unknown, Unknown]，用 cast 落定值类型
    return cast("dict[str, Any]", parsed)


def resolve_interface(name: str) -> Callable[..., Any]:
    """按名称解析接口函数，并校验接口存在性。

    接口不存在时通过 ak.interface_info 抛出异常，其消息会附带
    最接近的候选名，便于纠正拼写。注意 ak 命名空间下的元接口
    （search / interface_info / list_categories）不在注册表中，
    但同样可以动态调用，因此以 getattr 能否取到函数对象为准。

    Args:
        name: 接口名。

    Returns:
        可调用的接口函数。
    """

    # getattr 取不到才触发校验异常，异常消息自带候选名
    interface = getattr(ak, name, None)
    if interface is None:
        ak.interface_info(name)
        raise AssertionError("unreachable")
    return interface


def invoke(interface: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    """动态调用接口函数并返回原始结果。

    Args:
        interface: 接口函数对象。
        kwargs: 关键字参数。

    Returns:
        接口返回值，通常是 pandas.DataFrame。
    """

    return interface(**kwargs)


def frame_to_csv(frame: pd.DataFrame) -> str:
    """把表转换为 CSV 文本。

    Args:
        frame: 待转换的表。

    Returns:
        CSV 文本。
    """

    return frame.to_csv(index=False)


def frame_to_json(frame: pd.DataFrame) -> str:
    """把表转换为 JSON 文本。

    Args:
        frame: 待转换的表。

    Returns:
        JSON 文本。
    """

    # default=str 兜底 datetime.date 等非 JSON 原生类型
    return json.dumps(frame.to_dict(orient="records"), ensure_ascii=False, default=str)


def render_preview(frame: pd.DataFrame) -> str:
    """渲染表格预览文本。

    Args:
        frame: 待预览的表。

    Returns:
        前 N 行加数据规模说明的文本。
    """

    total = len(frame)
    preview = frame.head(PREVIEW_ROWS).to_string(index=False)
    if total > PREVIEW_ROWS:
        preview += f"\n\n（共 {total} 行，仅预览前 {PREVIEW_ROWS} 行）"
    return preview


def format_result(result: Any, output_format: str) -> str:
    """按指定格式把接口结果序列化为文本。

    Args:
        result: 接口返回值。
        output_format: print / csv / json 之一。

    Returns:
        序列化后的文本。

    Raises:
        ValueError: 不支持的输出格式。
    """

    if isinstance(result, pd.DataFrame):
        if output_format == "print":
            return render_preview(result)
        if output_format == "csv":
            return frame_to_csv(result)
        if output_format == "json":
            return frame_to_json(result)
        raise ValueError(f"不支持的输出格式: {output_format}")
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
    return str(result)


def main() -> None:
    """命令行入口，负责解析参数、动态调用并输出结果。"""

    parser = argparse.ArgumentParser(description="AKShare 接口动态执行")
    parser.add_argument("interface", help="接口名，如 stock_zh_a_hist")
    parser.add_argument(
        "--params", help='JSON 格式的参数对象，如 \'{"start_date": "20250101"}\''
    )
    parser.add_argument(
        "--format", choices=["print", "csv", "json"], default="print", help="输出格式"
    )
    parser.add_argument("--output", help="输出文件路径，不指定时输出到标准输出")
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
        kwargs = parse_params(args.params) if args.params else {}
        interface = resolve_interface(args.interface)
        result = invoke(interface, kwargs)
        text = format_result(result, args.format) if result is not None else None
    except Exception as exc:
        print(f"调用失败: {exc}", file=sys.stderr)
        sys.exit(1)

    if text is None:
        print("接口未返回数据")
        sys.exit(0)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"已保存 {len(text)} 字节到 {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
