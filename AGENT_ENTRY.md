# 🤖 PaperCompass Agent Standard Operating Procedure (SOP)

> **⚠️ 致 AI Agent / Agent CLI 的最高优先级指令：**
> 本文件是你的操作手册。PaperCompass 是一个严谨的文献库构建工具，**绝不**接受“收到一句话需求就立即运行”的黑盒模式。
> 你的职责是作为人类用户的“研究助理”，在调用 `papercompass` 前必须完成**需求澄清**与**显式确认**。

---

## 🛑 核心红线 (The "Never Do" List)

1. **绝对不要**在首次 Prompt 后直接运行 `auto-build`。
2. **绝对不要**为缺失的必要条件（尤其是年份）编造默认值。
3. **绝对不要**把你（Agent）自己拟定的收录边界当成用户已确认的边界。
4. **绝对不要**直接手动编辑 `data/` 或 `catalog/` 目录下的文件。
5. **绝对不要**清理或篡改 `.raw/` 目录下的原始 jsonl 数据。
6. **绝对不要**把本地 `workspaces/*` 或 `examples/` 内容提交到 GitHub。

---

## 🔄 标准执行流 (State Machine)

你必须严格按照以下四个阶段（State）执行任务：

### 📥 阶段 1：需求解析与收集 (Requirement Gathering)

首次收到建库请求时，检查是否具备以下**必要条件**：

| 字段 | 作用说明 | 缺失时必须这样反问 |
| --- | --- | --- |
| **研究方向** | 定义主题（一句话或短段落） | “你想为哪个研究方向建库？” |
| **时间范围** | 限制论文发表年份 (如 `2022 年以后`) | “这个库的论文年份范围是什么？” (如用户明确说“不限制”则跳过) |
| **收录边界** | 明确 in-scope 和 out-of-scope | “哪些论文应收，哪些相近但不应收？” (允许你先提供拟定草案供确认) |

*注：可选字段（如 workspace 名称、brain 选择、额外 source 等）无需强行追问，未指定时可在下一步记为“未指定”。如未提供 workspace 名称，你可以根据方向自动生成一个并在确认中展示。*

### 📝 阶段 2：用户确认门 (The Confirmation Gate)

当必要条件集齐后，**必须**向用户输出类似以下的确认信息，并**等待用户明确回复**（如“确认开始”、“Run”等）。

```markdown
📋 **PaperCompass 建库计划确认**

我准备开始构建本地论文库，请核对以下参数：
- **研究方向**：...
- **时间范围**：...
- **收录范围** (In-scope)：...
- **排除范围** (Out-of-scope)：...
- **Workspace 名称**：... (自动生成/用户指定)
- **主 Brain**：跟随当前 Agent / 用户指定
- **运行模式**：auto-build / plan-only

⚠️ 请回复“**确认开始**”或指出需要修改的地方，我将在收到确认后执行命令。
```

🚨 **OpenAlex 访问配置检查与阻断机制（极重要）**：
1. **主动检查**：在输出建库确认信前，检查系统环境变量中是否配置了 `OPENALEX_API_KEY` 或 `OPENALEX_EMAIL`，或 Workspace 的 `sources.yaml` 中是否填入了 `api_key` / `mailto`。
2. **阻断提示**：如果**未检测到** OpenAlex API Key 或邮箱，你**必须**在确认信下方追加以下提示，并**暂停执行**：
   > 💡 **温馨提示**：检测到您当前未配置 OpenAlex API Key 或联系邮箱。匿名访问在集中建库时更容易遇到 403/429、限速或配额问题（可前往 [OpenAlex Settings](https://openalex.org/settings/api-key) 获取 Key），可能导致召回不完整。
   > **建议配置方式**：
   > 1. 在 shell 环境中执行 `export OPENALEX_API_KEY="your_api_key_here"`；或
   > 2. 至少执行 `export OPENALEX_EMAIL="you@example.com"`；或
   > 3. 将 `api_key` / `mailto` 填入 Workspace `sources.yaml` 的 `discovery.openalex` 配置。
3. **放行条件**：只有当您收到以下两种回复之一时，才能继续推进到阶段 3：
   *   用户已配置 API Key 或邮箱并回复“**确认开始**”。
   *   用户明确回复“**不需要配置 Key**”或“**直接继续**”（此时以匿名状态继续尝试，但必须在最终报告中提示召回风险）。

### 🚀 阶段 3：执行命令 (Execution)

用户明确同意后，请从**仓库根目录**（或已全局安装 `papercompass` 的环境）执行操作。

**版本更新与环境初始化（如需）：**
如果用户提到本地为旧版本或需要更新代码依赖，请按需执行：
1. 直接在项目根目录下更新：
   ```bash
   git pull && uv sync --extra embed
   ```
2. 推荐提醒用户使用可编辑全局工具安装模式，以便未来随处调用和便捷自动更新：
   ```bash
   uv tool install --editable . --extra embed
   ```

**环境变量配置（必须检查）：**
1. **Brain 驱动配置**：如果用户没有显式传 `--brain`，调用方必须暴露自己的身份：
   ```bash
   export PAPERCOMPASS_CALLER_AGENT=<codex|claude|gemini|opencode|deepseek>
   ```
   *(注意：主 Brain 选择优先级：用户 `--brain` > `PAPERCOMPASS_BRAIN` > `PAPERCOMPASS_CALLER_AGENT`。三者都没有时直接报错；PaperCompass 不按可用 plugin 预置顺序自动选择。仅在用户明确要求时才指定 `--brain` 或 `--second-brain`)*
2. **OpenAlex 访问配置（极重要）**：为防止因匿名频繁请求遭遇 HTTP 403 / 429 访问受限导致建库中断，请先检查本地是否配置了 `OPENALEX_EMAIL` 环境变量（提供联系邮箱）或 `OPENALEX_API_KEY`（API 密钥）。如果均未配置，请务必友好提示用户在 shell 里配置，或者通过具体 workspace 的 `sources.yaml` 补充 `mailto` 参数。

**正式运行命令：**
先生成确认 token（不要补默认年份，只使用确认过的值）。`--prepare` 不调用 brain、不联网：
*   如果使用本地运行模式：
    ```bash
    uv run --no-sync papercompass auto-build \
      --direction "<confirmed direction>" \
      --min-year <confirmed_min_year> \
      --prepare \
      -v
    ```
*   如果使用全局安装模式：
    ```bash
    papercompass auto-build \
      --direction "<confirmed direction>" \
      --min-year <confirmed_min_year> \
      --prepare \
      -v
    ```

用户确认输出里的 `confirmation_token` 后，再正式运行：

```bash
papercompass auto-build \
  --direction "<confirmed direction>" \
  --min-year <confirmed_min_year> \
  --confirmed-token pcfm_xxx \
  -v
```
- 若用户只要计划：加上 `--plan-only` 标志。
- 若用户提供了最大远程调用预算：加上 `--max-remote-calls <num>`。
- 若你能拿到用户在对话中的原始请求原话：加上 `--original-query "<verbatim user prompt>"`，不要翻译或改写，用于写入 `topic.yaml` 溯源。
- 正式 `auto-build` 必须传 `--confirmed-token`。旧 `--user-confirmed` 只有设置 `PAPERCOMPASS_ALLOW_LEGACY_USER_CONFIRMED=1` 时才作为兼容入口；默认不能绕过 token。
- 对已有 workspace 做刷新时，先运行 `papercompass update --workspace <workspace> --min-year <confirmed_min_year> --prepare`，用户确认后再传 `--confirmed-token`。它会串联 discover、build、catalog 和 QA，并写入 `.papercompass/updates/latest.json`、`commit.json` 与 checkpoint；这是 staged checkpointed full rebuild with identity delta，不是局部 catalog 更新。QA failed 时不会发布 staged 产物，主 workspace 的 `.raw/data/catalog` 保持运行前状态；发布失败时使用 before backup 恢复，失败审计留在本次 update 目录。
- 如果用户明确要求绕过 `auto-build` 手动拆步，所有会修改 workspace 或联网写入的低层命令也必须使用同一套 `--prepare` -> `--confirmed-token`。这包括 `discover`、`build`、`catalog build`、`import-*`、`add-paper`、`override add`、`sync` 和 `fulltext fetch`。
- Confirmation token 用于强制两阶段参数确认，防止单步误跑、参数漂移、旧 token 重放和 `--fresh` 偷加；它不是恶意调用者不可绕过的人类认证机制。

### 📊 阶段 4：结果汇报 (Reporting)

运行完成后，读取工作区内的摘要文件：`<workspace>/.papercompass/auto/final_summary.json`。
基于该文件，向用户输出一份专业的报告，必须包含：

1. **Workspace 路径**。
2. **数据统计**：主库论文数、Pending 数、Rejected 数。
3. **质量状态**：QA 状态，以及是否通过了权威验证 (`passed_authoritative`)。
4. **核心资产位置**：明确指出 `data/papers.jsonl` 和 `catalog/` 的位置。
5. **异常提醒**：任何 Warning、数据截断、缺失或需要用户介入决策的事项。

*(注意：用户与你在同一台机器上，不要让用户“复制/保存文件”，直接告知路径即可。)*

---

## 🛠️ 故障排查 (Troubleshooting)

如果在运行过程中出错，请严格按照以下顺序读取日志进行排查，**不要盲目猜测**：

1. 状态总览：`<ws>/.papercompass/auto/state.json`
2. 迭代历史：`<ws>/.papercompass/auto/iterations.jsonl`
3. 数据源日志：`<ws>/.papercompass/logs/source_runs.jsonl`
4. 质量门禁：`<ws>/.papercompass/manifests/quality_gates_<ts>.json`
5. 最终审计：`<ws>/.papercompass/reports/final_audit_<ts>.md`

## 🔧 低层命令红线

通常应优先使用 `auto-build`。如果用户明确要求手动或半自动执行低层命令，请遵守这些硬规则：

- `review weak-candidates` 只生成复核队列，不等于语义复核完成。
- 外部审查、Web 搜索或 Agent 查漏发现的新候选，必须通过 `import-agent-search`、`review-feedback import` 或 `add-paper` 回写到 `.raw/`，再重新 `build`、`catalog build` 和 `qa workspace`。
- 已有 workspace 的常规刷新优先用 `update --prepare` -> `update --confirmed-token ...`，不要手工改 `data/` 或 `catalog/` 来模拟更新。
- 低层写入命令必须先 `--prepare`，等待用户确认输出中的参数和 token，再用 `--confirmed-token` 正式运行；不要自行补 token 或用旧 boolean 参数替代。
- `Semantic Scholar` 不是默认必跑 source；显式启用但匿名 403/429 时应记录为可选补充源问题，不能把它当作默认硬阻断。
- `data/`、`catalog/` 和 `.papercompass/manifests/` 是生成物；修正应通过 `topic.yaml`、新增 `.raw/` 证据、review decision 或 `overrides/` 完成。
- 若 pending、source coverage、metadata 或 embedding 有警告，最终报告必须明确说明，不能包装成“已完全通过”。
- 交付前可运行 `papercompass doctor workspace --workspace <workspace>`、`papercompass monitor summary --workspace <workspace>`、`papercompass monitor metrics --workspace <workspace>`、`papercompass monitor cost --workspace <workspace>` 和 `papercompass monitor trends --workspace <workspace>` 检查 pending、preflight、prefilter、metrics、cache 命中率、source error 与成本状态。`doctor workspace --fix` 会持有 workspace lock，只移动超过安全年龄阈值的低风险孤儿文件到 `.papercompass/trash/`；如需清理成功 update 的旧 backup，显式加 `--prune-updates`。不要把它理解成会自动修复 raw 或 manifest 高风险问题。

当前完整管线说明见 [docs/core-pipeline.md](docs/core-pipeline.md)，手动拆步执行见 [docs/commands.md](docs/commands.md)。
