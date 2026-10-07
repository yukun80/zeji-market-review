# ZeJi-market-review

用于A股收盘复盘、行业扫描、公司研究和候选池维护的技能包。先发现变化，再核对业务和价格，最后形成条件预案。免费优先取数，遇到困难时与人工协作；不自动交易。

## 开始使用

将完整技能目录交给支持技能加载的工具，然后说：

> 使用ZeJi-market-review复盘最近已完成的A股交易日，主动查询并阅读原文。缺资料时提出具体补充需求，保存复盘和候选变化。

本地取数需要Python 3.10+，在项目目录执行：

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe scripts/market_data.py doctor --network
```

Git Bash使用相同路径；macOS/Linux将解释器换为 `./.venv/bin/python`。脚本生成资料与计算结果，完整复盘仍需Agent阅读、核验和写作。

## 技能入口

| 内容 | 入口 |
|---|---|
| 十步复盘 | [技能说明](SKILL.md) |
| 研究核验 | [研究规则](references/research-rules.md) |
| 来源、参数与账号 | [工具池](references/tool-pool.md) |
| 补充材料后继续 | [人工协作](references/human-collaboration.md) |
| 填写模板 | [每日](assets/review-template.md) · [股票池](assets/pool-template.md) · [周末](assets/weekend-template.md) |
| 方法来源与示例 | [来源边界](references/source-map.md) · [虚构演练](examples/fictional-walkthrough.md) |

## 本地项目文档

[项目文档目录](docs/README.md)按使用、来源、协作、工作流程、验证和维护分章索引。

**docs和根目录AGENTS.md仅在本地保存，已排除在默认Git上传范围之外。GitHub副本不包含它们，本地文档链接在那里不可用。** 技能必需的说明和模板仍随包保留，不依赖docs。

个人复盘、缓存、独立环境和账号配置默认不上传。程序检查通过不等于全市场资料齐全，也不证明投资有效性。
