# PaperCompass Codebase Review & Optimization Strategy

## 背景介绍
我为你提供了一个名为 **PaperCompass** 的项目完整源码压缩包（`PaperCompass_source.zip`），已作为附件上传。

**PaperCompass** 是一个**为 AI Agent 打造的本地文献库构建工具**。
在学术调研和文献管理中，人机协作面临以下痛点：
1. **结果容易丢失**：有价值的论文散落在冗长的对话记录中，缺乏持久化。
2. **边界模糊不清**：哪些论文被收录、哪些被排除，往往缺乏证据和审查标准。
3. **难以二次利用**：无法轻松地将本次调研的结果喂给另一个 Agent 或 Web UI 继续使用。

PaperCompass 的目标是通过人机协作（User 💬 Agent 🤖 PaperCompass），将一个模糊的研究方向转化为一个结构化的 Workspace。候选论文、来源证据、筛选决策和质量检查将被完整地保存在本地。

### ⚠️ 关于 Agent 协作的定位（重点设计考量）
在本项目中，我们**不预先设定**用户拥有多个不同的 Agent（例如分别负责不同任务的 Agent 团队），也**不推荐**复杂的跨模型/多模型协作。
典型的用户使用方式非常直接：“**使用 papercompass 帮我调研……**”。
这意味着：
- 接收用户指令并调用 PaperCompass 的就是**当前的主 Agent（单一 Agent 场景）**。
- 虽然项目目前代码中包含“审计 Agent”的概念，或之前曾考虑过使用不同的模型进行交叉审计，但为了让用户最省事，我们希望在架构和流程上**尽可能只使用这一个主 Agent** 独立且简便地完成建库、复核、分类与 QA 的全套工作。

---

## 评审要求与核心问题
请你深度阅读上传的 `PaperCompass_source.zip` 源码（包括 `src/` 核心逻辑、`docs/` 设计文档以及 `tests/` 测试用例），并从以下六个维度给出深入、具体且具备工程指导价值的优化建议与意见。**请务必全程使用中文进行详细回复，结合具体文件和代码逻辑进行剖析：**

### 1. 架构设计与本地优先（Local-First）原则
- 目前的 Workspace 契约（`workspace_contract.py`）和目录结构（包含 `.papercompass/`, `data/`, `catalog/`, `sources.yaml`, `topic.yaml` 等）设计是否优雅？
- 在保持“本地优先、文件即数据（File-first）”的前提下，如何进一步增强数据读写的健壮性，防止由于并发调用或进程意外中断导致本地 JSONL 数据损坏？

### 2. 单一主 Agent 场景下的防呆与交互简化（Agent-First）
- `AGENT_ENTRY.md` 为 Agent 规定了严格的 SOP（例如 Confirmation Gate 和 OpenAlex API Key 阻断检查）。然而，外部 Agent 依然可能因为“幻觉”绕过 SOP 强行运行命令行。
- 我们如何通过 **CLI 和代码层面的防御性编程**（例如在 `cli.py` 或 `orchestrator.py` 中引入校验逻辑），强制或引导主 Agent 必须先获得用户确认？
- 在“只使用这一个主 Agent”的前提下，如何最大程度地简化交互，让建库流程对用户而言既省事又防呆？

### 3. `discovery.py` 模块化重构与数据源扩展
- `src/papercompass/discovery.py` 承担了极其庞杂的检索与发现逻辑，目前文件大小接近 150KB，有明显的重构空间。
- 请给出一个优雅的 **数据源可插拔架构（Pluggable Source Architecture）** 设计方案，将 OpenAlex、ArXiv、Gemini Search 甚至是 Semantic Scholar 拆分为独立的插件或类，以提高代码的单一职责性和可维护性。
- 在多源检索中，如何设计更高效的增量去重与数据对齐（Normalization）策略？

### 4. 单一 Agent 流程下的文献复核（Candidate Review）与 QA 门禁
- 审视 `candidate_review.py` 和 `qa.py` 中的 LLM 评分/判定逻辑。
- 我们希望**不依赖复杂的多 Agent 对质（Critique）或多模型分工**，只让同一个主 Agent 独自搞定文献质量把控。在这种限制下，如何通过 Prompt 优化、自我反思（Self-Reflection）或带有置信度的评分机制，最大化提升相关性评审和 QA 校验的准确率（Precision/Recall）？
- 如何通过优化上下文，降低单 Agent 重复调用的 Token 消耗和建库成本？

### 5. 确定性 Pipeline 与大模型（LLM/Agent）的职责边界划分
- 全部交给 LLM/Agent 处理虽然效果好，但会带来不可接受的响应延迟与经济成本。因此，核心原则是：**能够用确定性代码、算法或规则解决的问题，就绝不使用 LLM。**
- 请分析我们目前的设计与代码实现（如 `discovery.py`、`candidate_review.py`）：
  - 哪些目前使用了 LLM 的步骤，实际上可以通过**传统的确定性代码/算法**（例如：基于 TF-IDF/BM25 的关键词相关性打分、基于领域规则/白名单的过滤、基于引用数与期刊元数据的静态截断等）来完全替代？
  - 如何设计一个优雅的**前置过滤（Pre-filtering）流水线**，将文献发现后的大量脏候选进行初筛，使送入大模型（LLM Review）的候选文献数量降至最低，从而实现性价比最大化？

### 6. 增量更新与持续追踪（Continuous Monitoring）
- 学术文献是持续产出的。如果用户希望对某个已建好的 Workspace 进行 **增量更新（例如每周自动追踪并吸纳新发表的相关论文）**，目前的管线设计需要做哪些改造？
- 如何在不重复拉取和全量复核的前提下，由单一 Agent 优雅地整合新发现的文献，并自动修正 `catalog` 分类与 QA 状态？

---

## 期待的输出格式
1. **架构重构规划**：对 `discovery.py` 以及“确定性 Pipeline 与 LLM 职责划分”的具体重构方案或类图设计（可以用 Mermaid 表示）。
2. **核心代码改进**：针对重构后的插件机制、防御性 CLI 校验、确定性前置过滤流水线或 LLM 单 Agent 复核机制，给出具体的 Python 伪代码或实现片段。
3. **设计缺陷与建议**：列出您在代码中发现的**所有**可能引发 Bug、性能瓶颈、成本冗余或体验硬伤的设计缺陷，并给出解决方案。
4. **开放式发现（不限范围）**：**请不要局限于上述列出的问题**。如果您在阅读源码时发现了任何其他我们未提及的设计盲区、重构契机或潜在风险，请一并详细指出并给出优化建议。
