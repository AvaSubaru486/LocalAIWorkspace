# Local AI Workspace

**让已有电脑，成为本地 AI 实验平台。**

Local AI Workspace 是面向 Windows 的本地 AI 工作区，将 Qwen3 8B、Gemma 3 27B、Llama 3.3 70B 三档模型、推理运行环境、桌面聊天与运行日志整合为便携应用。项目围绕低配置设备展开：通过低比特量化、按需读取、内存预算和进程生命周期管理，探索有限内存中的大参数模型运行。

[项目博客](https://avasubaru486.github.io/blog/local-ai-workspace/) · [个人主页](https://avasubaru486.github.io/) · [完整便携包](https://github.com/AvaSubaru486/LocalAIWorkspace/releases/tag/portable-2026.10.28) · [答辩 PDF](presentation/LocalAIWorkspace-参赛答辩版-20261029.pdf)

## 可以做什么

- 在同一桌面界面完成文字聊天、历史会话、附件输入与 Python 工作区操作。
- 切换三档本地模型，管理请求取消、内存预算与模型进程回收。
- 使用 Gemma 3 27B 与配套视觉组件处理图片、分析视频抽样帧。
- 导出运行日志 ZIP，统一记录设备、模型哈希、首字时长、解码速度、内存及结束状态，供跨设备实验比较。
- 在包含运行时和模型的完整便携包中，解压后双击 `LocalAIWorkspace.exe` 启动。

## 三档模型

| 模型 | 本版量化 | 权重体积（十进制） | 运行路径 |
| --- | --- | ---: | --- |
| Qwen3 8B | UD-IQ2_XXS | 2.61 GB | CPU 内存映射，轻量文字交互 |
| Gemma 3 27B | UD-IQ1_S | 6.51 GB | CPU 内存映射，支持配套视觉组件 |
| Llama 3.3 70B | UD-IQ1_S | 15.85 GB | 分层 CPU 读取，大参数模型运行探索 |

27B 的视觉组件另占约 0.86 GB，完整 ZIP 为 **29,697,284,670 字节**。本地推理使用电脑自身资源；模型、推理后端与第三方依赖沿用各自的许可证。

## 下载与启动

GitHub 的“Code → Download ZIP”下载的是源码与展示材料。运行应用请前往 [Releases](https://github.com/AvaSubaru486/LocalAIWorkspace/releases/tag/portable-2026.10.28)，下载全部 **16 个 `.part01`—`.part16` 分片**，以及 `manifest.json`、`SHA256SUMS.txt`、`Merge-Portable.ps1`，放在同一目录。

在 PowerShell 中进入该目录，运行：

```powershell
.\Merge-Portable.ps1
```

脚本先校验分片，再合并原始 ZIP，最后核对整个 ZIP 的 SHA-256。校验成功后，将 ZIP 完整解压到 SSD 目录，双击 `LocalAIWorkspace.exe`。保留 `app`、`data`、`runtime` 等目录的原始相对位置，并为分片、合并文件与解压目录预留空间。

参考实验设备为 Intel Core i5-1235U、16 GiB RAM、Intel UHD 核显。配置文件为 70B 标注了 24 GiB 起、32 GiB 推荐内存；16 GiB 的记录属于受限条件下的运行实验，实际等待和内存需求随硬件、上下文与系统负载变化。

## 资料中记录的实机演示

以下为 **2026-10-29 答辩材料**中的一组短任务，参考机为 i5-1235U、16 GiB 内存、Intel 核显，三档模型串行运行；上下文 512 tokens，最多生成 64 tokens。

| 模型 | 实际输出 tokens | 后端解码 token/s | 首字时长 / 秒 | 整轮时长 / 秒 |
| --- | ---: | ---: | ---: | ---: |
| 8B | 16 | 4.261 | 13.48 | 17.23 |
| 27B | 12 | 1.163 | 37.23 | 47.55 |
| 70B | 10 | 0.057 | 153.98 | 330.14 |

输入为“请用不超过20个汉字，介绍本地AI的作用。”这些数字来自随项目提供的展示材料，本次上传未重新运行模型测速；10 月 28 日说明文档记录了另一组更短样本，比较时请保留各自的测试条件。27B 4 token/s 与 70B 0.5 token/s 是后续优化目标。

图片已有简单样例验证，视频路径最多抽取 6 帧，当前两次视频回答的完整性仍需改进；该路径分析抽样画面，不转写音频。低比特量化的持续生成质量和成对任务评估仍需继续测量。

## 仓库结构

```text
app/             桌面交互、模型调度、内存预算与运行日志
config/          固定模型版本、下载来源、SHA-256 与运行参数
scripts/         构建、校验与原项目的实验脚本
launcher/        Windows 启动器源码
resources/       项目资源与知识库来源配置
data/            模型清单及附带的知识库资料
docs/            使用指南、技术说明和第三方许可
documentation/   原始项目介绍与展板文案（Word）
presentation/    原始项目汇报、答辩 PPT 与 PDF
demo/            原理动画、三模型实机演示视频
```

源码仓库不包含大型模型、Python/Qt/CUDA 运行时或编译后的应用；这些内容保存在 Release 的原始便携包中。原始压缩包中的文档说明保留在 [docs/BUNDLED-README.md](docs/BUNDLED-README.md)。

## 项目材料

- [参赛答辩 PDF（2026-10-29）](presentation/LocalAIWorkspace-参赛答辩版-20261029.pdf)
- [参赛答辩 PPT（2026-10-29）](presentation/LocalAIWorkspace-参赛答辩版-20261029.pptx)
- [项目介绍 Word](documentation/LocalAIWorkspace-项目介绍-20261028.docx)
- [展板文案 Word](documentation/LocalAIWorkspace-项目展板文案-20261028.docx)
- [35 秒三模型实机演示](demo/LocalAIWorkspace-三模型实机演示-35秒-20261029.mp4)
- [项目原理动画](demo/LocalAIWorkspace-项目原理动画-20261028.mp4)

## 来源与许可

模型来自 Qwen、Google Gemma 和 Meta Llama，量化文件采用 Unsloth 发布版本；推理依赖 llama.cpp 等上游组件。项目贡献集中在桌面集成、运行路径组织、资源管理、离线交付与可复测日志。完整说明与各项上游条款见 [docs/LICENSES.md](docs/LICENSES.md)，不将第三方模型或运行库统一标为自有代码许可。
