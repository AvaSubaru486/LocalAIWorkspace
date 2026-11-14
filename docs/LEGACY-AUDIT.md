# 旧版压缩包静态审计记录

本文记录对用户提供的 `D:\chat ai test\LocalAIWorkspace-win-x64.zip` 的可复核静态检查。审计输入没有启动旧程序，也没有连接其可能使用的服务；下面的“存在”结论来自 ZIP 条目、PE/.NET 元数据、资源和 IL 字符串/调用引用。它们不能替代旧版端到端运行测试。

## 输入和可复核产物

- ZIP：`D:\chat ai test\LocalAIWorkspace-win-x64.zip`，14,517,116,799 字节，65 个条目。
- 主程序：`./LocalAIWorkspace.exe`，190,115,801 字节，SHA-256 `5cb466b6e3584eb5574577854cf7176c7bb145a5bb592de55932d3665c1e4d0c`。
- 内嵌 .NET 程序集位于文件偏移 59,469,824，长度 933,376 字节，SHA-256 `0e5db185fae13bb731855bebc460e18bba9035fae70dde4d6d3a8a22036bd730`；程序集名 `LocalAIWorkspace`，版本 `0.1.0.0`。
- WPF 资源包含 `mainwindow.baml`、`pages/workspaceview.baml`、`pages/modelsview.baml`、`pages/operations/operationsview.baml`、`pages/settingsview.baml` 和 `pages/wikipediaknowledgeview.baml`。
- 逐条读取 ZIP、PE 头、程序集表、资源表和 IL 引用的机器产物保存在 `build/legacy-static-audit.json`。该 JSON 是审计输出，不是产品运行时数据。

当前工作区保留旧文件副本：`legacy-backup/LocalAIWorkspace.exe`、旧 ZIP、`MODEL-MANIFEST.json` 以及 Holo、Qwythos、Spark 三个旧模型文件。新包构建白名单不会收集此目录。

## 从静态证据确认的旧功能

程序集包含以下服务和页面调用链，说明这些能力在旧版代码中有实现入口：

- `MainWindow.LoadConversationSummariesAsync` / `SaveConversationSummariesAsync` 读取 `conversation-summaries.json`；`WorkspaceConversationSnapshot` 保存标题、消息和 `Mode` 字段。当前版本迁移完整 `Messages`，并把旧模式转换为普通对话。
- `WorkspaceView` 包含 `StartNewTask`、`OpenHistoricalTask`、`Search_Click`、文件树/编辑器、终端运行、提案准备和审阅窗口入口。
- `LocalInferenceClient` 提供流式请求、模型切换、空闲卸载和指标事件；`LocalModelRuntimeService` 提供下载、启动、停止和健康检查。
- `ModelsView` 含模型搜索、下载、取消、删除、硬件扫描、速度校准和 Laya 制品入口。`DeployLayaArtifactAsync` 的字符串明确说明旧包只有权重部署，缺少 tokenizer/推理适配器，不应伪报可聊天推理。
- `WikipediaKnowledgeBaseService` 含 ZIM/JSONL/TXT 索引、Kiwix 工具、来源和 AI 读取开关；当前版本改为内置 libzim 的本地数据索引，并保留显式启用。
- `OperationsService` 含代理任务、Git 工作目录、代码审核、需求审核、验收命令和合并前指纹检查；`SkillCatalogService` 含技能目录与启用入口。
- `HardwareScanner`、`ContextCompressor`、模型建议与显存/内存配置类型均存在。

这些结论表示“代码和界面入口存在”。审计没有将旧 EXE 解压后启动，因此不把每一项描述成旧版已通过运行验证。

## 不应从旧包推导为新包默认能力的内容

旧 ZIP 的 65 个条目只有 `runtime/llama` 原生文件、WPF 单文件 EXE、三个旧 GGUF 模型和一个 0.4B safetensors 文件；没有 `runtime/python/python.exe`、PyTorch、AirLLM、transformers、libzim 或 FFmpeg。旧包因此不能作为当前“内置 Python、AirLLM、视频抽帧和 libzim”已经存在的证据。

旧模型条目为 Holo、Qwythos、Spark。它们已按当前产品要求从新包默认集合删除，原文件只在 `legacy-backup` 保存；当前默认模型由新 manifest 和自检验证 Qwen3-8B 及 0.4B 辅助模型。

旧程序集仍保留 `Mode`、`WorkspaceGoal`、`GoalCriterion` 及 `ModePicker_SelectionChanged` 等计划/目标模式类型和入口。当前普通对话改造删除这些 UI/API 路径；旧程序集中的名称不能作为新版本应保留模式系统的依据。

## 审计边界

静态审计未验证旧程序的网络请求、模型推理、下载完整性、GPU 行为、Kiwix 工具运行、代理任务合并或任何 Windows 驱动环境行为。当前版本的验收证据应来自当前源码测试、便携资源自检、桌面 smoke test 和实际打包后的干净目录检查；不能把本文件替代为这些验证。
