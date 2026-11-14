# Local AI Workspace

面向 Windows 的本地 AI 工作区，将三档模型、推理环境、桌面聊天、资源管理与运行日志整合为完整离线应用。

## 开始使用

完整解压 ZIP 到 SSD 目录，双击 LocalAIWorkspace.exe。应用、模型与运行库保持原有目录结构，即可在本机生成回答。

| 模型 | 量化 | 权重体积 | 运行方式 |
| --- | --- | ---: | --- |
| Qwen3 8B | UD-IQ2_XXS | 2.61 GB | CPU 内存映射 |
| Gemma 3 27B | UD-IQ1_S | 6.51 GB | CPU 内存映射 |
| Llama 3.3 70B | UD-IQ1_S | 15.85 GB | 分层 CPU 读取 |

完整离线包约 29.70 GB，包含 27B 视觉组件。图片与视频抽样帧进入同一 27B 模型；视频最多抽取 6 帧，当前回答完整性仍需改进。

## 主要功能

- 桌面聊天、历史会话、附件输入和 Python 工作区。
- 三档模型配置、资源预算、切换、取消与进程回收。
- 导出运行日志 ZIP，记录设备、模型哈希、首字时长、token/s、内存和结束状态。

## 使用与技术资料

- [使用指南](docs/beginner-guide/index.html)
- [作品介绍与同机部署对比](docs/PROJECT-INTRODUCTION.zh-CN.md)
- [运行结构图](docs/beginner-guide/assets/architecture.svg)
- [展板文案](docs/PROJECT-BOARD.zh-CN.md)
- [运行参数](docs/LOW-MEMORY.zh-CN.md)
- [依赖许可](docs/LICENSES.md)

## 目录

- app/：桌面应用、模型路由、进程管理和运行日志。
- config/models.json：模型来源、固定文件、SHA256 和运行参数。
- data/models/：三档模型及配套视觉组件。
- runtime/：Python、Qt、推理后端、FFmpeg 等运行环境。
- docs/：使用指南、作品介绍和技术资料。
