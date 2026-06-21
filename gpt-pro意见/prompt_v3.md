# PaperCompass Codebase Audit (Round 3) — 安全性收尾、确定性前筛与结构化评审重构审计

## 1. 背景与进展汇报

在上一轮咨询中，您对 **PaperCompass**（本地优先文献库构建工具）的并发文件锁缺陷、Windows 平台锁缺失、Workspace 并发踩数据、SOP 门禁漏网命令、Brain 解耦不彻底等问题提出了非常有深度的审计建议。同时，您设计了**确定性 Prefilter (BM25) 前置过滤流水线**、**两阶段确认令牌门禁**、**多因子指纹缓存机制**及**反证 Self-Reflection 反思逻辑**的重构蓝图。

针对您指出的所有设计漏洞与重构蓝图，我们刚刚在项目中完成了全面的代码编写、重构与回归测试。本次我们重新上传了最新的核心源码压缩包 **PaperCompass_source.zip**。

我们目前已完成以下核心功能重构，并且**全量 279 个 pytest 测试已全部顺利通过**：

### 1.1. 本地原子写入与 Workspace 锁重构
* **异常临时文件清理**：在 `text.py` 中重构了 `atomic_write_text()`，加入 `try...except BaseException`，确保任何环节异常中断时，均会自动 `unlink` 清理产生的 `.tmp` 残留临时文件，杜绝污染 Workspace。
* **跨平台锁 Fallback**：引入了 `portalocker` 库的 optional dependency，在 Windows 平台上提供了健壮的文件锁 fallback 机制；若不满足，且在 Windows 下并发写入时则会发出显式 warning，从而实现了健壮的跨平台 `append_jsonl_locked()`。
* **Workspace 级互斥写锁**：新增了全局 `workspace_lock` 机制，强制覆盖 `auto-build`、`update`、`discover`、`build`、`catalog build`、`import-papers` 等所有 mutating 级高危写操作命令，保障多进程/多智能体并发的一致性。
* **全系核心状态原子写**：将 `write_yaml()`、`AutoState.save()`（即 `state.json`）、`final_summary.json` 等写操作全部改为了原子替换写入，杜绝了进程中断时发生损坏/截断。

### 1.2. 令牌式 SOP 两阶段确认门禁
* 将原本的 Boolean `--user-confirmed` 门禁升级为了**两阶段令牌校验机制**。
* 运行 `--prepare` 时只输出待确认的变更细节 JSON 并生成存放在 `.papercompass/confirmations/pcfm_<hash>.json` 中的一次性确认令牌（此阶段绝对不调用 Brain，不联网，无费用）。
* 正式构建时必须显式携带 `--confirmed-token pcfm_...`，Orchestrator 将对其有效期、有效期状态、以及输入配置的哈希一致性进行严格验证，彻底防范了幻觉智能体自行假造确认指令。

### 1.3. 确定性 Prefilter 流水线接入
* 实现了 `DeterministicPrefilter` 核心类，并在 `build` 的去重/标准化（Dedupe & Normalize）后、LLM 评审排队前，正式接入该流水线。
* 基于 `TopicLexicon` 正负向匹配以及通过全量候选集拟合的 **BM25 Scorer** 对论文相关性进行启发式打分。
* 前筛动作严格定义为 `protected`（Seed 论文受保护）、`hard_reject`（硬过滤拒绝）、`reject`（普通确定性低相关拒绝）、`strong`（高确定性合格，仍进行比例抽样审计）与 `review`（送入 LLM Judge 的模糊区间）。
* 过滤决策全部落盘至 `data/prefilter_decisions.jsonl`，并为 QA 系统新增了 `prefilter_efficiency` 指标及过宽、过窄门禁告警。

### 1.4. 单主 Agent 结构化 Review 升级与指纹 Cache
* **Review Schema 数值化**：评审打分 Schema 中加入了数值型 `confidence`（0-1 精度）和 `paper_role`（论文角色，如 `core_method` 或 `boundary_negative`），支持输出 `inclusion_evidence` 与 `exclusion_evidence`。
* **指纹缓存 v2 落地**：设计了 `review_cache_key()`。Cache Key 不仅包含 candidate ID，还联合绑定了 `topic.yaml` 判定规则哈希、Prompt 版本、Schema 版本、策略指纹、以及当前调用的 Brain 品牌和模型名称，防范过期缓存复用。
* **边界反证 Self-Reflection**：引入自检模块，仅针对分数处于边缘区间或与前筛结果产生严重冲突的样本调起 Reflection。反思 Prompt 仅要求**寻找反证**（即如果是 accept，去寻找跑题证据；如果是 reject，去寻找在 scope 内的证据），避免重复泛读。

### 1.5. 通用 API 兼容 Brain 与 Discovery 插件化
* 新增了 `OpenAICompatibleBrain` 插件驱动类，允许通过通用的 `base_url_env`、`api_key_env` 和 `model` 自定义网络 API 交互，彻底与特定模型品牌解耦。
* 拆分了 Discovery 模块，并在 `sources/registry.py` 中实现了初步的数据源注册协议 skeleton，为后续逐源插件化迁移做好了底层架构准备。

---

## 2. 本轮审计与咨询核心诉求

请您阅读我们本次重构后的最新代码包，特别是并发锁与临时文件垃圾回收（`text.py`）、两阶段确认令牌设计（`cli.py`, `orchestrator.py`）、确定性前筛流水线（`prefilter.py`）、多因子指纹缓存与边界 Reflection 自省机制的实现代码，并重点回答以下核心问题（请全程使用**中文**进行详细剖析）：

### 2.1. 评估本轮更改的代码实现质量
* 我们对 Workspace 并发锁、Windows 锁 fallback 以及原子写入异常清理的实现是否足够安全可靠？是否有隐藏的边界竞态条件或未回收的句柄？
* 两阶段确认令牌（`--prepare` + `--confirmed-token`）在防止幻觉 Agent 绕过门禁上是否达到了高安全强度？在错误日志和机器解析上是否具备足够高的可用性？
* 确定性 Prefilter 与多因子指纹缓存的集成代码是否符合设计契约？`DeterministicPrefilter` 中的 BM25 赋权与启发式过滤逻辑是否合理？
* 在 `OpenAICompatibleBrain` 与边界 Reflection 方面，我们做出的改动是否达到了预期效果？是否彻底解决了与特定模型品牌绑定和全量重复 LLM 判定的开销？

### 2.2. 指导我们下一步的重构与开发规划
* 针对我们接下来的开发规划，我们需要重点推进 **“Discovery 模块的插件化正式迁移”**，以及 **“Catalog/QA 增量更新与可回滚 checkpoint 架构”**：
  * 在当前已落地的 `sources/registry.py` 基础上，各具体数据源（如 arXiv, OpenAlex, Semantic Scholar）应如何优雅地完成业务逻辑解耦，并接入统一的插件注册、鉴权和 rate-limit 预检？
  * 针对增量式差分更新（Delta-only update），在 Discovery Checkpoint 提交、Review Cache 命中、以及 Catalog 局部修正方面，您对我们的数据目录与更新状态机有何更具体、科学的设计建议？
