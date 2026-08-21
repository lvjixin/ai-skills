# AI Skills

Claude Code 技能集合仓库，供 [CC Switch](https://github.com/farion1231/cc-switch) 等工具安装使用。

## 技能列表

| 技能 | 目录 | 说明 |
|---|---|---|
| [akshare](akshare/) | `akshare/` | AKShare 金融数据库接口检索与执行：内置检索层定位 1000+ 接口，动态调用并落盘 CSV/JSON；附带市场资金流与基金持仓分析报告 |
| [python-specifications](python-specifications/) | `python-specifications/` | 通用 Python 编码规范：依赖选型、代码风格、类型安全、静态检查 |

## 安装方式

### CC Switch

添加自定义仓库：`https://github.com/lvjixin/ai-skills`，按需填写 Subdirectory（如 `akshare`），然后在技能列表中点击安装。

### 手动复制

```bash
# 安装 akshare 技能到全局技能目录
git clone git@github.com:lvjixin/ai-skills.git
cp -r ai-skills/akshare ~/.claude/skills/
```

## 环境要求

- Python >= 3.14
- [uv](https://docs.astral.sh/uv/)（脚本依赖通过 uv 管理，首次执行 `uv run` 自动安装）
