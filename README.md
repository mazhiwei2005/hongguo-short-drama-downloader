# 红果短剧下载器 · 破解三集限制版

一个从红果短剧（hongguoduanju.com / 红果 App 后端）批量下载短剧全集的工具，**突破了网页端免费用户只能看/下前 3 集的平台限制**，App 通道可直下全集 **1080P** 并自动解密。

本项目为**个人学习 / 离线研究用途**，请阅读文末合规声明。

---

## ✨ 功能特性

- 🚀 **突破 3 集限制**：逆向红果官方 App 接口（X-Gorgon / X-Khronos 签名 + 随机设备身份），绕过网页端"免费只开放前 3 集"的限制，直接拿全集
- 🔓 **自动解密**：spade_a 密钥 + CENC (AES-128-CTR) 解密加密流，输出标准可播放 MP4
- 📺 **全集 1080P**：自动选择最高画质的分辨率视频流
- 🖥️ **三种下载通道**：App 通道（破解全集）、官网直连（前 3 集极稳）、通用 m3u8（第三方苹果 CMS 短剧站）
- 🎯 **多种取剧方式**：粘贴分享链接 / 详情页链接 / 剧名搜索 / series_id 直接下载
- 📁 **按剧分文件夹**：`第01集.mp4 … 第N集.mp4`，自动跳过已下载，支持断点续传(`.part`)
- 🔁 **失败自动补下**：失败的集写入 `_失败清单_<sid>.txt`，`--retry` 一键重下
- ⚖️ **防检测**：API 串行预取 + 集间随机延时 + 自适应并发（失败自动降并发 3→2→1）+ CDN 指数退避重试
- 🎛️ **Windows 双击菜单**：`红果下载.bat`，无需命令行基础

---

## 📦 一键使用（Windows）

双击 `红果下载.bat`（首次会弹窗选保存位置，之后回车沿用上次位置）。

```
菜单选项：
[1] 粘贴分享链接下载（可多链接，空格/逗号/换行分隔）
[2] 按剧名搜索选剧
[3] 输入 series_id 批量下载
[4] 选集下载（sid + 起始集数 + 截止集数）
[5] 补下失败集
[6] 单集测试
[9] 安装依赖（pycryptodome）
[Q] 退出
```

依赖只在首次自动安装（清华源），平时无需联网装包。

---

## 🖥️ 命令行使用

```bash
# 下载整部
python hgapp_dl.py --sid 7684151693751110681

# 只下第 5~10 集
python hgapp_dl.py --sid X --start 5 --end 10

# 粘贴官网详情页链接
python hgapp_dl.py --url "https://hongguoduanju.com/detail?series_id=X"

# 按剧名搜索选剧
python hgapp_dl.py --name "曝光神话"

# 解析红果 App 分享链接
python hgapp_dl.py --share "https://novelquickapp.com/s/xxxx"

# 补下失败清单里的集
python hgapp_dl.py --sid X --retry

# 只测第 1 集（快捷验证网络/密钥）
python hgapp_dl.py --sid X --test [--start N]

# 指定输出目录 + 集间延时
python hgapp_dl.py --sid X --out "D:/短剧" --delay 3
```

命令行通用参数：

| 参数 | 说明 |
|------|------|
| `--sid` | series_id（可逗号分隔，按顺序依次下载多部）|
| `--url` | 红果官网详情页链接（多链接依次下）|
| `--name` | 按剧名关键词搜索选剧 |
| `--share` | 红果 App 分享链接（短链或落地页）|
| `--start` / `--end` | 下载的起始 / 截止集数 |
| `--out` | 输出根目录（默认 `./downloads`）|
| `--delay` | 集间接口延时秒（默认 2）|
| `--retry` | 只补下失败清单里的集 |
| `--test` | 只下载 `--start` 那一集验证 |
| 裸参数 | 直接拖入/传入链接或 sid，自动识别 |

---

## 🧩 文件说明

| 文件 | 通道 | 说明 |
|------|------|------|
| `hgapp_dl.py` | **App 通道（破解全集）** | 逆向红果 App 接口，X-Gorgon 签名 + 随机设备，全集 1080P + 自动解密，**核心文件** |
| `HGDJ/client.py` | App 通道 | X-Gorgon/X-Khronos 签名算法、设备模拟、App API 客户端、网页数据抓取 |
| `HGDJ/crypto.py` | App 通道 | spade_a 密钥解码 + CENC (AES-128-CTR) MP4 解密（MP4 box 解析）|
| `hgweb_dl.py` | 官网直连 | 走 `hongguoduanju.com` 官网播放页，提取明文 MP4 直链（免费前 3 集，极稳无风控）|
| `hongguo_downloader.py` | 通用 m3u8 | 面向苹果 CMS(conch 模板) 第三方短剧站的批量 m3u8 下载 |
| `红果下载.bat` | — | Windows 双击菜单入口 |

`hgapp_dl.py` 的剧名搜索 / 分集列表会复用 `hgweb_dl.py` 的官网抓取逻辑。

---

## 🔬 核心原理（App 通道如何破解 3 集限制）

红果网页端对免费用户每部剧只开放前 3 集（第 4 集起网页播放页返回 404），这是平台级限制。本工具的破解绕开了网页端：

1. **详情页拿全部分集**：从官网详情页 `_ROUTER_DATA` 里解析出整部剧全部集的 `vid_list`，不受 3 集限制
2. **App 签名接口求播放地址**：用 `HGDJ/client.py` 构造红果 App 的签名请求（`X-Gorgon` / `X-Khronos` / `X-SS-STUB`，设备 ID / install ID / 指纹随机生成），请求 `/novel/player/video_model/v1/` 拿返回的 `main_url`（CDN 直链）和 `spade_a`（加密密钥）
3. **下载 + 解密**：下载加密 MP4，用 `HGDJ/crypto.py` 从 `spade_a` 解码出 AES-128 密钥，按 `senc` 里每采样 IV 用 CTR 模式解密 `mdat`，输出完整可播放的 MP4

> ⚠️ 因为是**逆向的签名密钥**，若红果更新 App 签名算法（`X-Gorgon`），本工具可能失效，届时需重新逆向。

---

## 📝 合规声明

- 本工具**仅供个人离线学习、分镜研究使用**，请勿用于商业用途、二创搬运、公开传播
- 下载内容的版权归原作者 / 红果平台所有，使用者自行承担相关责任
- 批量抓取可能违反平台服务条款，请**控制请求频率**、仅用于个人学习
- 需要完整可播放的资源，请优先支持正版

---

## ⚙️ 运行环境

- Python 3.8+（仅标准库 + `pycryptodome`）
- `pycryptodome` 用于 AES 解密（首次运行自动安装，或 `pip install pycryptodome`）
- ffmpeg 仅 `hongguo_downloader.py`（通用 m3u8 通道）需要，自动探测常见安装目录

## 🐞 常见问题

- **缺 1 集**：多半是 CDN 网络抖动，重跑加 `--retry` 会自动补下（已内置 3 次指数退避重试 + 自动重取播放地址）
- **406 / 风控**：App 通道会自动切换随机设备身份重试（最多 3 次）；网页通道较稳
- **找不到 ffmpeg**：仅 m3u8 通道需要，放到 `D:\EVCapture\ffmpeg.exe` 或 `C:\ffmpeg\bin\` 即可