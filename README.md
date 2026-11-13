# media-toolkit

多平台**无水印视频/图文下载** + **直播自动录制** 的 Python 命令行工具。

支持抖音、B站、小红书及 yt-dlp 覆盖的 1000+ 站点；Cookie 从浏览器**自动提取**，无需手动 F12 复制。

## ✨ 功能特性

- **无水印下载**：抖音视频自动解析无水印直链；图文作品下载全部原图（自动排除水印版和压缩图），文案同步存档为 txt
- **Cookie 自动化**：自动从 Edge / Chrome / Firefox / Brave 浏览器提取登录态，彻底告别手动复制 Cookie
- **直播录制**：开播自动录、下播自动停、断线自动重连，支持按时长分段，ts 格式断电不损坏文件
- **抖音直播可录**：内置纯 Python 的 a_bogus 签名（无需 Node.js），能取到原画 flv/hls 直链
- **多平台**：抖音 / B站 / 小红书 专用适配，其余 1000+ 站点由 yt-dlp 兜底
- **批量下载**：整段分享文案直接粘贴，自动提取链接、去重、批量下载
- **规范命名**：统一 `平台_作者_标题_时间戳` 命名并分类存储

## 🚀 快速开始

环境要求：Python 3.9+，以及 [ffmpeg](https://ffmpeg.org/download.html)（录制与高画质合并必需）。

```bash
# 1. 克隆并安装
git clone https://github.com/Yjnshy/github-yjnshy.git
cd github-yjnshy
pip install -e .                 # Cookie 自动提取已内置（复用 yt-dlp 的浏览器解密）

# 2. 生成自己的配置（可选，都有默认值）
cp config.example.yaml config.yaml

# 3. 开始用
mtk download "https://v.douyin.com/xxxxxxx/"     # 下载抖音无水印视频
mtk record https://live.douyin.com/123456789     # 录制一个直播间
mtk watch                                        # 值守 config.yaml 里的所有直播间
```

> 首次使用抖音功能前，请先在你选择的浏览器（默认 Chrome）里登录 douyin.com。
> Cookie 会自动提取并缓存 7 天，期间无需重复登录，也不用关闭浏览器。

## 📖 使用说明

### 下载 `mtk download`

```bash
mtk download "链接1" "链接2" -q 720 -o D:/视频
```

- 支持整段分享文案：`mtk download "7.94 复制打开抖音… https://v.douyin.com/xxx/ …"`
- `-q` 画质：`best`（默认）/ `1080` / `720` / `480` / `audio` / `mp3`
- 抖音图文作品会自动下载全部无水印原图，并保存文案 txt

### Cookie 相关

```bash
mtk --browser firefox download "链接"   # 临时换一个浏览器提取 Cookie
mtk cache                              # 查看缓存状态
mtk cache --clear                      # 换账号 / Cookie 失效时清空缓存
```

获取优先级：`cookies.txt`（配置指定）→ 本地缓存（7 天）→ 浏览器自动提取。

> **关于 Edge 读不到 Cookie**：Edge 与新版 Chrome 的部分版本启用了
> App-Bound Encryption（系统级密钥保护），第三方程序无法解密。
> 遇到 `Failed to decrypt with DPAPI` 时，请换用 Chrome / Firefox，
> 或导出一份 cookies.txt 在配置里指定 `cookies.cookies_file`。
>
> **关于浏览器正在运行**：Chrome/Edge 运行时会锁住 Cookie 数据库，
> 首次提取可能需要完全退出浏览器；成功提取后会缓存 7 天，之后不受影响。

### 录制 `mtk record`

```bash
mtk record https://live.bilibili.com/6                       # 录到断流/下播为止
mtk record https://live.bilibili.com/6 --segment 30          # 每 30 分钟切一段
mtk record https://live.douyin.com/123456789 -o D:/录制       # 指定保存目录
mtk record https://live.bilibili.com/6 --format mp4          # 容器格式（默认 ts）
```

立即开始录制单个直播间，实时显示已录时长与体积，Ctrl+C 立即停止（会先杀掉 ffmpeg 再退出，不留孤儿进程）。

抖音直播间支持这几种链接，直接粘哪个都行：

```
https://live.douyin.com/311923254809
https://www.douyin.com/follow/live/311923254809?anchor_id=...
https://www.douyin.com/root/live/311923254809
```

录制特性：

- **拉流透传 UA / Referer / Cookie** —— 抖音等平台的 CDN 会校验请求上下文
- **Cookie 自动裁剪** —— 完整 Cookie 有 7~8KB，超过 ffmpeg 的请求头上限会报
  `overlong headers` 直接打不开流，这里只保留拉流需要的少数几项
- **断流自动重连** —— ffmpeg 自带重连，重连失败由监控层指数退避重试（10s → 20s → … 上限 5 分钟）
- **失败不静默** —— 没录到数据（403、流地址过期）会抛出原因并删除 0 字节文件，不会留一堆空文件
- **ts 容器** —— 断电或强杀进程，已录部分仍可播放

> **抖音直播的实现说明**：抖音 Web 端已不再把房间数据写进页面 HTML，
> 改为必须请求 `webcast/room/web/enter` 接口，且查询串要带 `a_bogus` 签名，
> 否则接口返回空（风控）。本项目内置了一份纯 Python 的签名实现，
> **不依赖 Node.js**。抖音可能不定期更换算法，若突然报「接口返回空响应」，
> 说明签名已过期，需要同步上游实现（见下方「致谢」）。

### 值守 `mtk watch`

在 `config.yaml` 的 `record.rooms` 里每行填一个直播间链接（`#` 开头暂停监控），然后：

```bash
mtk watch
```

开播自动录、下播自动停、断线自动续录，7×24 无人值守；每 60 秒播报一次录制进度。
Ctrl+C 会停掉所有正在进行的录制再退出。

## ⚙️ 配置说明

复制 `config.example.yaml` 为 `config.yaml`，主要选项：

| 选项 | 说明 | 默认值 |
|---|---|---|
| `output_dir` | 保存根目录 | `./downloads` |
| `quality` | 默认画质 | `best` |
| `cookies.browser` | 从哪个浏览器提取 Cookie | `chrome` |
| `cookies.cookies_file` | 手动指定 cookies.txt（优先级最高） | 空 |
| `cookies.cache_days` | Cookie 本地缓存天数（0 关闭） | `7` |
| `proxy` | 网络代理 | 空 |
| `record.format` | 录制格式 | `ts` |
| `record.poll_interval` | 开播检测间隔（秒） | `60` |
| `record.max_retries` | 单场断线重连上限 | `20` |
| `record.segment_minutes` | 按时长分段（0 不分段） | `0` |

## 🔒 隐私与安全

- Cookie 只在你本机内存、系统临时目录和 `~/.media-toolkit/cookie_cache.json` 中流转，**不会上传任何地方**
- 缓存可通过 `mtk cache --clear` 随时清除
- `config.yaml` 与 `cookies*.txt` 已被 `.gitignore` 排除，不会误提交
- 请勿把含 Cookie 的文件发给任何人，那等同于你的账号登录态

## 🏗️ 项目结构

```
src/media_toolkit/
├── cli.py               # 命令行入口
├── core/                # 配置、日志、命名、ffmpeg、跨平台兼容
├── cookies/             # 浏览器 Cookie 自动提取
├── platforms/           # 平台适配层（douyin / generic 兜底）
│   └── douyin_sign.py   # 抖音 a_bogus 请求签名（纯 Python）
├── downloader/          # 下载引擎、图文下载
└── recorder/            # 直播监控与录制
```

## 🙏 致谢

- **[DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)**（MIT）：抖音直播接口的调用方式与
  `a_bogus` 签名算法参考自该项目，`platforms/douyin_sign.py` 即其实现的移植（已按 MIT 协议保留署名）
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)**：下载引擎与浏览器 Cookie 解密，覆盖 1000+ 站点
- **[FFmpeg](https://ffmpeg.org/)**：转封装、合并与录制

## ⚠️ 免责声明

本工具仅供**个人学习与技术研究**使用。请遵守各内容平台的服务协议与版权规定：

- 下载/录制的内容版权归原作者所有，请勿用于商业用途或未经授权的二次分发
- 请尊重创作者的劳动成果与隐私，不要录制、传播禁止录制的内容
- 使用本工具产生的一切法律后果由使用者本人承担，与项目作者无关

## 📄 License

[MIT](LICENSE)
