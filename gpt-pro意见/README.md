# PaperCompass GPT 5.5 Pro 意见获取指南

本项目已在此目录下准备好所有必需材料，供您使用 `oracle` 与 GPT 5.5 Pro 进行深度咨询。

## 📁 目录文件清单

1. **`PaperCompass_source.zip`**: 本项目源码的打包压缩包（已排除 `.git/`, `.venv/`, `workspaces/`, `logs/` 等无关的大文件与敏感信息，文件大小约 5.2 MB）。
2. **`prompt.md`**: 精心设计的 GPT-5.5 Pro 咨询提示词，涵盖：
   - 架构设计与本地优先（Local-First）原则
   - Agent-First 交互设计与防御性编程
   - `discovery.py` 模块化重构与数据源扩展
   - 基于大模型的文献复核（Candidate Review）与 QA 门禁
   - 持续追踪与增量更新机制
3. **`README.md`**: 本使用指南。

---

## 🚀 如何使用 Oracle 进行咨询

根据您的运行环境，请选择以下适合的命令进行首次调用与追问。

### 1. 检查 Chrome Remote Debugging 连通性
启动前，请确保您的 Windows 侧已开启带 remote debugging 的 Chrome 并登录了 ChatGPT 账号。然后在 WSL 终端运行以下命令验证连接：
```bash
curl -sS --max-time 2 http://127.0.0.1:9222/json/version
```
*注：如不通，请先确认 Windows 侧 Chrome 启动参数与端口。*

### 2. 首次启动咨询（发送 Prompt 并上传代码包）
在当前 `/home/yhli/yhli-local-projects/PaperCompass/gpt-pro意见` 目录下，运行以下命令：
```bash
oracle --engine browser --model gpt-5.5-pro --remote-chrome 127.0.0.1:9222 \
  --browser-archive never \
  --browser-auto-reattach-delay 5s --browser-auto-reattach-interval 3s --browser-auto-reattach-timeout 60s \
  --browser-attachments always \
  --slug papercompass-pro-review \
  -p "$(cat prompt.md)" --file PaperCompass_source.zip
```
*   `--browser-attachments always`：确保代码压缩包以真实文件附件形式上传至 ChatGPT 界面。
*   `--browser-archive never`：常规调用显式不归档会话，保持 Tab 开启，方便后续追问。
*   `--slug papercompass-pro-review`：指定 3 个单词的 Slug 标识符。

---

## 🔄 之后如何在同一个对话中反复询问（跨 Run 续接）

既然您可能需要反复在此对话中追问，请**在首次调用完成后不要关闭浏览器中对应的 ChatGPT 标签页**。

您可以直接在终端运行以下续接命令，利用 `--browser-tab current` 复用当前打开的会话 Tab 续答：

```bash
oracle --engine browser --model gpt-5.5-pro --remote-chrome 127.0.0.1:9222 \
  --browser-archive never \
  --browser-tab current \
  --browser-model-strategy current \
  --slug papercompass-pro-review-followup \
  -p "您的追问提示词"
```

*   `--browser-tab current`：表示复用当前处于活动状态的 ChatGPT 会话标签页。
*   `--browser-model-strategy current`：沿用当前会话的模型策略，不再重新触发复杂的 Selector 判定。
*   您也可以直接在打开的 ChatGPT 浏览器页面中手动输入追问，或通过此命令批量发送。
