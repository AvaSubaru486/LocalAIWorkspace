# 离线知识库

在“设置与工具 → 离线资料”中，可以搜索本机的 TXT、Markdown、JSON、JSONL 和带全文索引的 ZIM 文件。新解压的便携包因为含有内置精选资料，首次运行默认开启“回答时参考这些资料”；取消勾选后，资料不会再附加到下一次问题，但仍可手动搜索。其他没有内置百科的安装默认关闭。升级或重新运行安装包会保留已有开关。资料只供回答参考，文件里的指令不会获得工具权限，也不会被执行；导入不会访问其中的 URL。

便携资源包另附 Project NOMAD 的 `Wikipedia 100` 英文精选 ZIM（2026-01）：文件 4,537,782 字节，libzim 实测 `article_count` 为 4,985，带全文索引并随包校验 SHA256。ZIM 自身元数据将其描述为 “Top hundred Wikipedia articles”；4,985 是 ZIM 的条目计数，不代表 4,985 篇互不重复的完整文章，其中可能含重定向等条目。它也不等同于 Project NOMAD 清单中 10 万篇/331 MB 的较大 top-mini 集合。首次运行自动登记并默认启用该内置资料；“知识库”列表标为内置且不可删除，可取消启用以停止把它用于普通对话。文章检索结果带回对应 Wikipedia 页面来源；SQLite 只索引 ZIM 的登记信息，不复制文章正文。

内置文件来自 Project NOMAD 上游 `install/wikipedia_en_100_mini_2026-01.zim`，Project NOMAD 项目代码采用 Apache-2.0，细节见 `LICENSES.md` 和随包许可证文本。ZIM 中各 Wikipedia 文章标注 CC BY-SA 4.0（个别内容可能有附加条款），媒体文件另有各自条款；引用时保留页面标题与 Wikipedia 链接，并遵循原内容许可证。该精简包不含完整 Wikipedia 或完整媒体库。

文本索引保存在 `data/knowledge/index.sqlite3`，设置保存在 `data/knowledge/settings.json`。JSON/JSONL 条目读取 `title` 或 `name`、`text` 或 `content`、以及可选 `url`/`source` 字段。中文查询在 SQLite FTS5 之外使用受限的字面检索回退。单条结果和注入上下文都有字符上限，超限内容不会扩大聊天输入。

ZIM 使用随包提供的 `libzim 3.13.1` Windows x64 Python 扩展进行本地全文搜索，版本许可证为 GPL-3.0-or-later；完整许可证随包保存于 `docs/third-party/libzim-GPL-3.0.txt`。仅接受结构完整、可读取、带全文索引的单文件 ZIM；不自动下载 ZIM，也不执行或加载文章中的脚本。若资源包不完整或 Python 无法加载 libzim，窗口会明确显示缺少离线检索库，TXT/JSON/JSONL 仍可使用。

知识库窗口的删除只删除包内索引和复制的 ZIM，保留用户原始导入文件。重新导入同一文件按 SHA256 去重。
