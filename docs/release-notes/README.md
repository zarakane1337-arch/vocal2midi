# Release 文案

每个版本一份 GitHub Release 正文草稿。

| 文件 | 对应版本 |
|---|---|
| `v1.0.0.md` | 转写引擎换成 SwiftF0 |
| `v2.0.0.md` | 重构为 `src/vocal2midi/` 包 + 一键安装包 |

## 与 CHANGELOG 的关系

正文内容来自根目录的 [`CHANGELOG.md`](../../CHANGELOG.md)，本目录只在末尾追加
发布专用的小节（校验值、下载附件说明、Full Changelog 链接）。

**改动顺序固定：先改 CHANGELOG，再把对应版本的整段同步到这里。**
不要直接改这里的正文，否则下次同步会被覆盖。
