# Source Plugin Protocol（roadmap，未实现）

PaperCompass 当前的数据源（paperlists / OpenAlex / Crossref / DBLP / arXiv / ACL Anthology / PubMed / Europe PMC / OpenReview / Semantic Scholar）是硬编码在 `papercompass/discovery.py` 里的。本文档勾勒一个未来可能的 source plugin 抽象，让用户可以追加自定义源（bioRxiv、IEEE Xplore、HAL 等）而不改核心代码。

**当前状态**：未实现。本文档是设计草稿，不是约束。要先动 source plugin 之前，请先看 `papercompass/discovery.py` 的现状以免重复。

## 目标

让用户在 `sources.yaml` 里这样声明：

```yaml
discovery:
  sources: [paperlists, openalex, biorxiv]
  biorxiv:
    plugin: papercompass.contrib.biorxiv:BiorxivSource
    config:
      categories: [neuroscience, bioinformatics]
      max_pages: 2
```

`papercompass discover` 启动时按 `plugin:` import string 加载 callable，传 `config` 跑。

## 接口（设计草稿）

```python
class SourcePlugin:
    name: str

    def __init__(self, workspace: Path, topic: dict, config: dict, *, budget: RemoteBudget): ...

    def fetch(self) -> SourceResult:
        """returns dict with 'source', 'runs', 'seen', 'kept', 'errors'.
        Same shape as existing openalex_search / arxiv_search / etc."""
        ...

    def cache_keys(self) -> Iterable[str]:
        """每个细粒度 fetch 单元的稳定 key，用于 source_coverage 报告。"""
        ...
```

约束：

- 必须服从 `RemoteBudget`：每次真实 HTTP 调用前 `budget.bump(...)`，预算耗尽时抛固定异常以触发 graceful skip。
- 必须自带缓存路径，写入 `.papercompass/cache/discovery/<source-name>/...`。
- 必须落 `source_runs.jsonl`，schema 见现有 `record_source_run`。
- 必须把候选论文 wrap 成统一的 `wrap_candidate(...)` 输出，写入 `.raw/<source-name>/`。

## 不在范围内

- 不接管 build / classification / catalog。Source plugin 只关心**抓取 + 缓存 + `.raw/` 落盘**，下游一律用现有逻辑。
- 不允许 plugin 改 topic.yaml 或 applied_decisions.jsonl。

## 需要先做的事

1. 把 `discovery.py` 的 source 各自抽成模块（`papercompass/sources/openalex.py` 等），现状只有 `papercompass/sources/arxiv.py`。
2. 定义 `RemoteBudget` 的公共接口（目前 class 是 internal 的）。
3. 一个 contrib 例子（最简单的：从 RSS feed 抓某个领域的预印本）。

## 何时优先级会上来

- 用户跑非-CS 方向（医学、经济、社会科学），现有 paperlists 完全无效
- 多个用户/项目要同时维护非 arXiv 数据源
- 出现新的开放 API（如新的 OpenReview-like 服务）
