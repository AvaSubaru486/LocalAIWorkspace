# 第三方组件与许可证

再分发便携包前应保留所有组件的许可证、版权说明和对应源码获取途径。模型下载/导入不代表自动获得商业使用或再分发许可。上游条款适用于实际发布版本。

| 组件 | 许可证 / 来源 |
| --- | --- |
| CPython 3.11.9 | PSF License；包内 runtime/python/LICENSE.txt；https://www.python.org/downloads/release/python-3119/ |
| Microsoft Visual C++ Runtime | Microsoft 可再分发运行库条款；使用已验证 Microsoft 数字签名的 DLL，应用本地部署；https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files |
| PyTorch 2.6.0 CUDA 12.4 | BSD 风格许可证与 NVIDIA CUDA 第三方许可；torch 包内 LICENSE 与 third-party notices；https://pytorch.org/ |
| PySide6-Essentials / shiboken6 6.8.3 | 发行包 METADATA 声明 LGPL-3.0-only 或 GPL-2.0-only 或 GPL-3.0-only；包内 docs/third-party/Qt-LGPL-3.0.txt、Qt-GPL-2.0.txt、Qt-GPL-3.0.txt；https://doc.qt.io/qtforpython-6/licenses.html |
| Qt 6.8.3 | 各 Qt 模块及其第三方代码按对应模块许可证；完整 Essentials wheel 中包含未用于本程序的模块，不能将所有文件统一标为 LGPL；对应源码见 https://download.qt.io/archive/qt/6.8/6.8.3/submodules/ 与 https://code.qt.io/cgit/pyside/pyside-setup.git/?h=6.8.3 |
| llama.cpp | MIT；docs/third-party/llama-license.txt；https://github.com/ggml-org/llama.cpp ，构建 b11435 |
| FFmpeg Gyan essentials | 构建包含 GPL 组件，不能仅标注 LGPL；随包保留 LICENSE。构建源与脚本：https://www.gyan.dev/ffmpeg/builds/ ，上游源码：https://ffmpeg.org/download.html |
| AirLLM 4.0.0 | Apache-2.0；https://github.com/lyogavin/airllm |
| python-libzim 3.13.1 / libzim 9.8.2 | GPL-3.0-or-later；包内 `docs/third-party/libzim-GPL-3.0.txt` 与 `runtime/python/Lib/site-packages/libzim-3.13.1.dist-info/licenses/LICENSE`；对应包装器源码：https://github.com/openzim/python-libzim/tree/v3.13.1 ；底层库源码：https://github.com/openzim/libzim/tree/9.8.2 |
| Project NOMAD Wikipedia 精选 ZIM（`Wikipedia 100`，2026-01；4,985 个 ZIM 条目计数） | 数据文件来自 https://github.com/Crosstalk-Solutions/project-nomad/blob/main/install/wikipedia_en_100_mini_2026-01.zim ；Project NOMAD 软件仓库 Apache-2.0，条款见 `docs/third-party/Project-NOMAD-Apache-2.0.txt`。ZIM 元数据描述为百篇精选，4,985 是 libzim 的条目计数，不代表互不重复的完整文章数。文章依据各页面声明采用 CC BY-SA 4.0（或页面列出的附加条款）；保留文章标题及 Wikipedia 页面链接作为来源，并按 `docs/third-party/CC-BY-SA-4.0.txt` 与具体页面条款处理。媒体文件按各自许可处理。 |
| Creative Commons Attribution-ShareAlike 4.0 International | 完整许可文本随包提供于 `docs/third-party/CC-BY-SA-4.0.txt`；具体文章可能列出其他/附加条款。 |
| Git for Windows 2.55.0.windows.5 | Git 核心 GPL-2.0；包内 `runtime/git/LICENSE.txt` 及 `runtime/git/usr/share/licenses/`、`runtime/git/mingw64/share/licenses/` 中的组件许可证；对应版本源码：https://github.com/git-for-windows/git/tree/v2.55.0.windows.5 ，发行来源：https://git-scm.com/download/win |
| 随包模型：Qwen3-8B GGUF | Apache-2.0；data/models/qwen3-8b/LICENSE；https://huggingface.co/Qwen/Qwen3-8B-GGUF |
| 随包模型：Gemma 3 27B GGUF | Google Gemma Terms of Use；使用和再分发前核对条款，https://ai.google.dev/gemma/terms |
| 随包模型：Llama 3.3 70B GGUF | Meta Llama 3.3 Community License；使用和再分发前核对条款，https://www.llama.com/llama3_3/license/ |
| FastAPI / Uvicorn / HTTPX / python-docx / openpyxl / python-pptx | 具体版本的 MIT/BSD 类许可保留在各 dist-info 目录 |
| Pillow / pypdf / NumPy / SciPy / transformers / bitsandbytes | 保留各发行包内许可证与第三方说明 |

当前便携包仅预置上述三款模型权重，不包含 Laya 辅助权重、视觉模型、Gemma 4、Ministral、Mixtral 或其他历史实验模型。包内 Python `Lib/site-packages/*.dist-info` 的许可证目录不应从分发包中删去。FFmpeg、Git、libzim 及其随带组件应保留相应的许可证与源码来源，发布者需按实际分发方式履行对应源码提供义务；此文档不替代完整许可证。

桌面使用 Qt 动态库并保留源码及运行库目录，用户可替换相应兼容动态库。PySide6 wheel 中的 `LicenseRef-Qt-Commercial.txt` 只说明商业许可证选项，不代表本项目取得商业 Qt 授权；附带的 GNU 许可证正文来自 Qt 6.8.3 上游源码的 `LICENSES` 目录。发布者应按实际分发的 Qt/PySide 模块核对开源许可证、修改告知、源码与第三方声明义务。模型、FFmpeg、Qt 的上游条款各自适用。
