# Clio 小白安装教程

这份教程只假设你会下载文件、打开终端和复制命令。Clio 不要求购买域名或 VPS；先在本机跑通，再决定是否需要公网。

## 一、先选一种安装方式

| 你的需求 | 推荐方式 | 是否需要域名 | 电脑关机后能否使用 |
|---|---|---:|---:|
| 只在自己的电脑上使用 | 本地安装 | 不需要 | 不能 |
| 同一 Wi-Fi 下用手机管理 | 本地安装 + 局域网配置 | 不需要 | 不能 |
| 手机随时访问、AI 远程连接 | VPS 安装 + HTTPS | 推荐 | 可以 |

新手建议先选第一种。确认记忆写入、检索和管理页正常后，再迁移 VPS。

## 二、共同要求

- 64 位 Windows、macOS 或 Linux。
- Docker Desktop，或 Linux 上的 Docker Engine 与 Compose 插件。
- 推荐 4 GB 以上可用内存，Windows/macOS 更推荐 8 GB 总内存。
- 至少预留约 5 GB 磁盘空间；模型、镜像、快照和长期记忆会继续占用空间。
- 能访问 Docker 镜像和模型下载源的网络。

第一次构建需要下载基础镜像、Python 依赖和本地向量模型，可能耗时数分钟到更久。以后启动通常会快很多。

## 三、Windows 安装

### 第 1 步：安装 Docker Desktop

1. 打开 [Docker Desktop Windows 官方安装页](https://docs.docker.com/desktop/setup/install/windows-install/)。
2. 安装 Docker Desktop。
3. Windows 推荐启用 WSL 2 后端。
4. 重启后打开 Docker Desktop，等它显示引擎正在运行。

如果提示没有虚拟化：先检查 BIOS 中的 Intel VT-x / AMD-V，再检查 Windows 的“虚拟机平台”和 WSL 2。

### 第 2 步：下载 Clio

会用 Git：

```powershell
git clone https://github.com/dan521627-hash/Clio-Memory.git
cd Clio-Memory
```

不会 Git：

1. 在 GitHub 页面点击绿色 **Code**。
2. 点击 **Download ZIP**。
3. 解压到空间充足的位置，例如 `D:\Clio-Memory`。
4. 进入解压后的文件夹，在地址栏输入 `powershell` 并回车。

### 第 3 步：启动

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

第一次会构建镜像，并显示一个随机网页管理密码。先把密码保存在自己的密码管理器里。

### 第 4 步：确认成功

浏览器打开：

```text
http://127.0.0.1:8787
```

看到 Clio 登录页后，输入刚才生成的管理密码。

再打开：

```text
http://127.0.0.1:18001/health
```

看到 `status: ok` 代表记忆服务正常。

## 四、macOS 安装

### 第 1 步：安装 Docker Desktop

从 [Docker Desktop 官方下载页](https://docs.docker.com/get-started/introduction/get-docker-desktop/) 选择自己的芯片版本：

- Apple Silicon：M1、M2、M3、M4 等。
- Intel Mac：选择 Intel 版本。

安装并启动 Docker Desktop。

### 第 2 步：下载并启动

打开“终端”：

```bash
git clone https://github.com/dan521627-hash/Clio-Memory.git
cd Clio-Memory
chmod +x start.sh
./start.sh
```

然后在 Safari 或其他浏览器打开 `http://127.0.0.1:8787`。

## 五、Linux 或 VPS 安装

建议使用 Ubuntu 22.04/24.04 LTS。先按照 [Docker Engine 官方文档](https://docs.docker.com/engine/install/) 安装 Docker 与 Compose 插件。

```bash
git clone https://github.com/dan521627-hash/Clio-Memory.git
cd Clio-Memory
chmod +x start.sh
./start.sh
```

默认端口只绑定 `127.0.0.1`，这是安全设计。VPS 上直接访问公网 IP 的 8787 端口不会打开。

若要公网使用，需要额外配置：

1. 自己的域名（推荐，但不是程序本身的必需品）；
2. HTTPS 反向代理或 Cloudflare Tunnel；
3. 防火墙；
4. 管理页强密码；
5. 定期备份 `data/`；
6. 不把 SSH 密钥、隧道凭据或 `.env` 上传到 GitHub。

## 六、配置 API（可选）

系统不填 API Key 也能启动。需要更完整的摘要、状态裁判、事实候选、待办抽取和推送文案时，再配置一个 OpenAI 兼容模型。

1. 打开本机 `.env`。
2. 填写自己的 Key：

```text
OMBRE_API_KEY=你自己的Key
```

3. 打开 `config.yaml`，在 `dehydration` 中填写服务商提供的模型名和 `base_url`。
4. 重启：

```bash
docker compose up -d --force-recreate
```

不要把 Key 写进 Python、README、截图或提交记录。

## 七、配置 Bark（可选，仅 iPhone/iPad）

Bark 是独立的 iOS 推送应用，不是 Clio 必需组件。安卓用户可正常使用 Clio 管理页，但本仓库当前没有内置安卓推送渠道。

1. 在 iPhone/iPad 安装 Bark，取得自己的设备 Key。
2. 在 `.env` 填写：

```text
OMBRE_BARK_DEVICE_KEY=你自己的设备Key
```

3. 先保持 `config.yaml` 中 `behavior.mode: rehearsal` 做演习。
4. 确认推送文案和时间规则正确后，再改成 `live`。

不要分享完整 Bark 地址，它等同于推送凭据。

## 八、让手机打开管理页

### 使用 VPS

配置好自己的 HTTPS 域名后，iPhone、iPad、安卓手机和平板都可以直接用浏览器打开。网页是响应式的，不要求安装 App。

### 只在家里局域网使用

默认 Compose 只允许本机访问。要开放给同一 Wi-Fi 的手机，需要自行修改端口监听、设置电脑防火墙，并使用电脑的局域网 IP。

这会扩大访问范围。不了解防火墙时，不建议直接修改为全网监听。更简单的选择是继续只在电脑使用，或部署到带 HTTPS 的 VPS。

手机网页与主动通知的兼容区别见 [手机访问与消息推送](PUSH-ZH.md)。

## 九、接入 MCP 客户端

本机地址：

```text
http://127.0.0.1:18001/mcp
```

远程地址应当是你自己配置的 HTTPS 完整路径：

```text
https://你的域名/mcp
```

如果客户端报错，依次检查：

1. `docker compose ps` 中两个服务是否都在运行；
2. `/health` 是否返回正常；
3. 客户端是否支持 Streamable HTTP MCP；
4. 公网代理是否保留 MCP Session Header；
5. 地址末尾是否确实包含 `/mcp`。

## 十、备份、迁移和卸载

### 最重要的备份

停止写入后备份：

- `data/`
- `.env`
- `config.yaml`
- 如有自定义裁判书，也备份对应私人 JSON

### 停止但保留数据

```bash
docker compose stop
```

### 删除容器但保留本地数据

```bash
docker compose down
```

### 真正删除

只有手动删除 `data/` 才会移除本地记忆。执行前必须先确认备份。

## 十一、常见故障

### Docker 报虚拟化未检测到

检查 BIOS 虚拟化、WSL 2、虚拟机平台和 Windows Hypervisor Platform。

### 第一次启动很慢

正在下载镜像、依赖或向量模型。先耐心等待，并检查磁盘空间与网络。

### 管理页打不开

运行 `docker compose ps`，再检查 `http://127.0.0.1:8787`。不要把 MCP 地址当成普通网页打开。

### 电脑关机后 AI 连不上

本地版必须依赖电脑开机。需要 24 小时在线时迁移到 VPS。

### 手机能不能直接承载服务器

本项目没有提供 iOS/Android 原生服务器包。手机用于浏览器管理；服务器运行在电脑或 VPS。
