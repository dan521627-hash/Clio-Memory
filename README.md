# Clio Memory（Clio 记忆系统）

> 给长期陪伴型 AI、个人助手和跨窗口对话准备的自托管 MCP 记忆与状态系统。

Clio 的名字来自希腊神话中掌管历史的缪斯。它并不是要把 AI 困在过去，而是让下一次对话有地方继续，让写下来的事实、感受、约定和未完成的事不再随着窗口关闭而散掉。

本仓库是经过隐私清理的公开空壳：**没有任何真实记忆、姓名、人物设定、域名、API Key、推送密钥、管理密码、数据库、日志或备份。** 下载者会得到同一套系统和工具，但记忆从空白开始。

## 先用大白话理解它

Clio 像一座由 AI 和使用者共同维护的记忆小屋：

- **记忆书房**保存事件原文，不让摘要偷偷覆盖原文。
- **信箱**保存上一个窗口写给下一个窗口的话。
- **开机摘要**只交付少量核心目录、最新流动信息和当前真正需要处理的内容，避免记忆越多，开机越烧 Token。
- **智能检索**同时理解关键词和相近意思。即使措辞不同，也能找到相关记忆。
- **激素、心念、暗涌、共振与张力**不是生物学结论，而是一套可检查的状态模型：写入的事件会形成状态，状态会影响想起什么、沉默期间形成什么念头，以及可选的主动推送。
- **记忆日历、事实时间线、未竟事项**让记忆不只按主题保存，也能按日期、事实变化和待办状态查找。
- **安全层**负责写前快照、追加写入、冲突警告、封存、钉选、验真暗语和只演习不执行的自动整理。
- **网页管理小屋**让不懂代码的人也能查看、修改、封存、删除、搜索和导出自己的记忆。

## 这套系统最特别的地方

它不是简单的“聊天记录搜索器”，而是一条彼此联动的链：

```text
AI 写下一件事或一封信
        ↓
原文安全保存，并自动记录北京时间
        ↓
建立语义向量、主题、日期、关联和可选的事实/待办候选
        ↓
事件与感受进入状态评估，影响激素、心念、共振和张力
        ↓
沉默或静默期间形成暗涌，并可按规则产生 Bark 推送
        ↓
下一个窗口调用 pulse_boot，只收到核心目录和最新交接
        ↓
需要细节时再用 breath 搜索、recall 分页读取原文
```

这意味着：记忆、状态、检索和下一次见面不是四套互不认识的功能，而是同一件事从“发生”到“保存”、从“沉淀”到“再次被想起”的不同阶段。

详细说明见：

- [完整功能与联动说明](docs/FEATURES-ZH.md)
- [小白安装教程](docs/INSTALL-ZH.md)
- [使用方式与常见问题](docs/FAQ-ZH.md)
- [手机访问与消息推送](docs/PUSH-ZH.md)
- [隐私与安全边界](docs/PRIVACY.md)
- [技术架构](docs/ARCHITECTURE.md)

## 能在哪些设备上用

| 设备 | 能否运行服务器 | 能否打开管理页 | 说明 |
|---|---:|---:|---|
| Windows 10/11 电脑 | 可以 | 可以 | 安装 Docker Desktop，推荐 WSL 2 后端 |
| macOS 苹果电脑 | 可以 | 可以 | Intel 与 Apple 芯片均可安装对应 Docker Desktop |
| Linux 电脑 / VPS | 可以 | 可以 | 安装 Docker Engine 与 Compose 插件 |
| iPhone / iPad | 不建议 | 可以 | 作为手机管理端使用，需要局域网地址或公网 HTTPS 地址 |
| 安卓手机 / 平板 | 不建议 | 可以 | 与苹果手机相同，浏览器打开管理页即可 |

**苹果能用，安卓也能用。** 但要分清两件事：

- Mac 是电脑，可以在本地运行整套 Clio。
- iPhone 和安卓手机通常只是管理和访问入口，真正的服务仍运行在电脑或 VPS 上。

主动通知目前默认接入 Bark，因此 iPhone/iPad 可以接收；安卓可以正常使用网页管理和 MCP，但当前版本还没有内置安卓主动推送。详细区别见 [手机访问与消息推送](docs/PUSH-ZH.md)。

## 下载前需要准备什么

### 必须准备

1. 一台电脑或 VPS。
2. Docker 与 Docker Compose。
3. 一个支持 MCP Streamable HTTP 的 AI 客户端。
4. 至少预留约 5 GB 磁盘空间；长期使用还要为记忆、快照和导出留余量。

### 不一定需要

- **不需要先买域名**：只在本机使用时，直接使用本地地址。
- **不需要先买 VPS**：电脑保持开机时，可以完全在本地运行。
- **不需要 API Key 才能启动**：没有外部模型时，记忆存取、网页管理和基础检索仍可运行；智能摘要、情绪裁判、自动候选等能力会受限。
- **不需要 Bark**：手机主动推送是可选功能，默认使用演习模式，不会真的发送。

## 三分钟启动

### Windows

先安装并启动 [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)，确认 Docker 显示正在运行。然后：

```powershell
git clone https://github.com/dan521627-hash/Clio-Memory.git
cd Clio-Memory
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

没有 Git 也可以在 GitHub 点击 **Code → Download ZIP**，解压后在文件夹空白处打开 PowerShell，再执行最后一行。

### macOS / Linux / VPS

```bash
git clone https://github.com/dan521627-hash/Clio-Memory.git
cd Clio-Memory
chmod +x start.sh
./start.sh
```

第一次启动会自动：

1. 从示例生成私人 `.env` 和 `config.yaml`；
2. 随机生成网页管理密码和返回验真暗语；
3. 创建空的数据、模型与导出目录；
4. 构建并启动记忆服务与网页管理服务。

启动完成后：

- MCP：`http://127.0.0.1:18001/mcp`
- 管理页：`http://127.0.0.1:8787`
- 健康检查：`http://127.0.0.1:18001/health`

管理密码会在第一次启动时显示，也保存在本机 `.env` 文件中。**不要把 `.env` 发给别人。**

## 本地运行和 VPS 运行有什么区别

### 本地版

- 记忆只保存在自己的电脑里。
- 默认只有这台电脑能访问，最省事也最安全。
- 电脑关机或休眠后，AI 暂时无法连接。
- 如果要让同一 Wi-Fi 下的手机访问，需要额外配置局域网监听和防火墙。

### VPS 版

- 服务可以 24 小时在线，电脑关机也不影响。
- 手机和不同设备更容易访问。
- 需要自己购买并维护 VPS；推荐再配置域名、HTTPS、备份和防火墙。
- **不要直接把 8787 或 18001 端口裸露到公网。**

完整部署选择见 [小白安装教程](docs/INSTALL-ZH.md)。

## 接入 AI

本地 MCP 地址：

```text
http://127.0.0.1:18001/mcp
```

如果 AI 客户端与 Clio 不在同一台电脑上，需要把 MCP 服务放到安全的公网 HTTPS 地址后再接入，例如：

```text
https://你的域名/mcp
```

不同 AI 产品配置 MCP 的位置不同，但需要填写的是同一个完整 MCP 地址。客户端必须支持远程 MCP；仅支持本地命令型 MCP 的客户端需要额外桥接。

建议把 [CLAUDE_PROMPT.md](CLAUDE_PROMPT.md) 中的使用规则加入客户端说明，让 AI 知道什么时候调用 `pulse_boot`、`breath`、`recall`、`hold` 和 `grow`。

## 数据放在哪里

- `data/`：记忆 Markdown、SQLite 辅助数据库、状态与历史。
- `models/`：本地向量模型缓存。
- `exports/`：从管理页生成的导出文件。
- `.env`：API Key、管理密码、验真暗语和可选推送密钥。
- `config.yaml`：功能开关、阈值和裁判配置路径。

这些目录和文件默认不会被 Git 上传。真正迁移或重装时，最重要的是备份 `data/` 和私人配置，而不是只备份 Docker 镜像。

## 重要注意事项

1. **AI 读过记忆不会清空记忆。** `breath` 和 `recall` 是读取操作。
2. **外部 API 可能接触到送去评估的文字。** 是否配置 API Key，要根据服务商隐私政策自行决定。
3. **本地向量检索不等于所有功能都完全离线。** 语义向量可以本地运行，但配置外部模型后，摘要、情绪裁判等请求会发给对应服务商。
4. **封存不是加密。** 封存内容默认不检索、不展示，但磁盘上的数据仍需靠系统权限和磁盘加密保护。
5. **删除前先导出。** 永久删除会真正释放数据，不应依赖历史快照兜底。
6. **自动消化默认只演习。** 系统只给出整理建议，不自动合并、归档或删除记忆。
7. **公开分享前检查隐私。** 不要上传 `.env`、`config.yaml`、`data/`、数据库、日志、备份、截图或私人提示词。
8. **这是实验性个人记忆系统。** “激素”“心念”“暗涌”等名称是计算模型和叙事隐喻，不代表 AI 具有生物激素或被科学证明的主观意识。

## 更新与停止

查看运行状态：

```bash
docker compose ps
```

停止服务：

```bash
docker compose stop
```

重新启动：

```bash
docker compose up -d
```

更新代码前先备份 `data/` 和 `.env`，再拉取新版本并重新构建：

```bash
git pull
docker compose up -d --build
```

## 项目来源与许可证

Clio Memory 基于 [P0lar1zzZ/Ombre-Brain](https://github.com/P0lar1zzZ/Ombre-Brain)（MIT License）改造。原始许可证保留在 [LICENSE](LICENSE) 中，修改后的代码继续按 MIT License 开放。

English summary: Clio Memory is a self-hosted MCP memory, continuity, and inspectable state system. The Chinese documentation above is the primary user guide; code and configuration keys remain readable in English.
