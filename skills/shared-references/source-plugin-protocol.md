# Source Plugin Protocol（当前实现与扩展约定）

PaperCompass 当前已有 `papercompass.sources.registry` 和 `SourcePlugin` 协议。所有内置 source 都通过 registry 进入 discovery；arXiv、OpenAlex、Semantic Scholar 已走结构化 `plan_queries -> fetch -> normalize` runner。Crossref / DBLP / ACL Anthology / PubMed / Europe PMC / OpenReview / paperlists / Gemini Search 仍保留稳定 sync 兼容层，但也暴露同一协议方法，便于后续逐步迁移。

**当前状态**：registry、preflight、协议方法和三类 structured runner 主路径已实现；自定义第三方 plugin 的动态 import 仍是 roadmap。改 source 前优先看 `src/papercompass/sources/registry.py`、`src/papercompass/discovery.py::run_structured_source_plugin()` 和已有 `sources/arxiv.py`、`sources/openalex.py`、`sources/semantic_scholar.py`。

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

动态 `plugin:` import 尚未开放；当前内置 source 由 `default_source_registry()` 注册。

## 接口

```python
class SourcePlugin:
    name: str
    capabilities: SourceCapabilities

    def preflight(self, context: DiscoveryContext) -> SourcePreflight: ...

    def plan_queries(self, context: DiscoveryContext) -> list[SourceQuery]: ...

    def fetch(self, query: SourceQuery, context: DiscoveryContext) -> list[dict]: ...

    def normalize(self, item: dict, query: SourceQuery, context: DiscoveryContext) -> dict: ...

    def run(self, context: DiscoveryContext) -> dict:
        """兼容层入口；未迁移 source 可以委托旧 sync_xxx。"""
        ...
```

约束：

- 必须服从 `RemoteBudget`：每次真实 HTTP 调用前 `budget.bump(...)`，预算耗尽时抛固定异常以触发 graceful skip。
- structured runner 会统一写 `.papercompass/cache/discovery/<source-name>/...`、`.papercompass/logs/source_runs.jsonl`、`.papercompass/manifests/source_coverage.json` 和 `.raw/<source-name>/`。
- `normalize()` 只返回 PaperCompass candidate raw shape；不要在 plugin 里改 `topic.yaml`、review decisions 或 catalog。

## 不在范围内

- 不接管 build / classification / catalog。Source plugin 只关心**抓取 + 缓存 + `.raw/` 落盘**，下游一律用现有逻辑。
- 不允许 plugin 改 topic.yaml 或 applied_decisions.jsonl。

## 需要先做的事

1. 将 Crossref / DBLP / paperlists 迁入 structured runner。
2. 将领域源（ACL Anthology / PubMed / Europe PMC / OpenReview）迁入 structured runner。
3. 设计自定义 `plugin:` import、版本约束和安全边界。
4. 增加 contrib 例子（最简单的：从 RSS feed 抓某个领域的预印本）。

## 何时优先级会上来

- 用户跑非-CS 方向（医学、经济、社会科学），现有 paperlists 完全无效
- 多个用户/项目要同时维护非 arXiv 数据源
- 出现新的开放 API（如新的 OpenReview-like 服务）
