# 帅羊羊DLY

> 想留的视频和直播，都收进自己的盘里。

多平台**无水印视频/图文下载** + **直播自动录制** 的工具，提供**桌面窗口**与命令行两套用法。

支持抖音、B站、小红书及 yt-dlp 覆盖的 1000+ 站点；Cookie 从浏览器**自动提取**，无需手动 F12 复制。

<sub>图标是透明底的卡通小羊：橙角、蓝眼、粉腮红、两只小蹄子，桌面浅色/深色背景都立得住。
源图 `packaging/icon_source.jpg`，重跑 `python packaging/make_icon.py` 即可重生成图标与界面 logo。</sub>

## ✨ 功能特性

- **桌面窗口**：独立窗口应用（不是网页），左侧导航切换下载 / 录制 / 值守，双击即用；同一套界面将来可直接复用到手机端
- **无水印下载**：抖音视频自动解析无水印直链；图文作品下载全部原图（自动排除水印版和压缩图），文案同步存档为 txt
- **自定义帧率**：下载与录制都能指定目标帧率（60/50/30/25/24/15），也可保持原帧率直通不转码
- **Cookie 自动化**：自动从 Edge / Chrome / Firefox / Brave 浏览器提取登录态，彻底告别手动复制 Cookie
- **直播录制**：开播自动录、下播自动停、断线自动重连，支持按时长分段与定时停止（到点自动停或自动续录下一段），ts 格式断电不损坏文件
- **抖音直播可录**：内置纯 Python 的 a_bogus 签名（无需 Node.js），能取到原画 flv/hls 直链
- **多平台**：抖音 / B站 / 小红书 专用适配，其余 1000+ 站点由 yt-dlp 兜底
- **批量下载**：整段分享文案直接粘贴，自动提取链接、去重、批量下载
- **规范命名**：统一 `平台_作者_标题_时间戳` 命名并分类存储

## 🚀 快速开始

环境要求：Python 3.9+，以及 [ffmpeg](https://ffmpeg.org/download.html)（录制、合并与转码必需）。

```bash
# 1. 克隆并安装
git clone https://github.com/Yjnshy/github-yjnshy.git
cd github-yjnshy
pip install -e ".[desktop]"       # 带桌面窗口；只要命令行用 pip install -e .

# 2. 生成自己的配置（可选，都有默认值）
cp config.example.yaml config.yaml

# 3. 开始用
dly gui                                          # 打开桌面窗口
dly download "https://v.douyin.com/xxxxxxx/"     # 下载抖音无水印视频
dly record https://live.douyin.com/123456789     # 录制一个直播间
dly watch                                        # 值守 config.yaml 里的所有直播间
```

> **关于桌面窗口**：界面用系统自带的 WebView2 渲染（Win10/11 默认已装），
> 不占额外内存也不用装 Qt。窗口关掉就等于退出程序，正在录制的任务会先被停止，
> 不会留下后台进程。没有 WebView2 时程序会自动改用系统浏览器打开，功能完全一样。

> 首次使用抖音功能前，请先在你选择的浏览器（默认 Chrome）里登录 douyin.com。
> Cookie 会自动提取并缓存 7 天，期间无需重复登录，也不用关闭浏览器。

## 📖 使用说明

### 下载 `dly download`

```bash
dly download "链接1" "链接2" -q 720 -o D:/视频
dly download "链接" --fps 30            # 下载完成后转成 30 帧
dly download "链接" --fps 24 --keep-original   # 转码，同时保留原片
```

- 支持整段分享文案：`dly download "7.94 复制打开抖音… https://v.douyin.com/xxx/ …"`
- `-q` 画质：`best`（默认）/ `1080` / `720` / `480` / `audio` / `mp3`
- `--fps` 目标帧率：不填 = 保持原帧率（下载完直接存，最快）
- `--keep-original` 转码后把原片另存为 `文件名_原画.mp4`
- 抖音图文作品会自动下载全部无水印原图，并保存文案 txt

> **关于帧率**：源视频的帧率是固定的，改帧率只能重新编码，所以是在下载**完成之后**
> 做一次转码。1080P 的 3 分钟视频通常需要几十秒到几分钟，取决于电脑性能；
> 转码会重新压缩画面（体积可能略微变大），只想收藏原片时请保持「原帧率」。

### Cookie 相关

```bash
dly --browser firefox download "链接"   # 临时换一个浏览器提取 Cookie
dly cache                              # 查看缓存状态
dly cache --clear                      # 换账号 / Cookie 失效时清空缓存
```

获取优先级：`cookies.txt`（配置指定）→ 本地缓存（7 天）→ 浏览器自动提取。

> **关于 Edge 读不到 Cookie**：Edge 与新版 Chrome 的部分版本启用了
> App-Bound Encryption（系统级密钥保护），第三方程序无法解密。
> 遇到 `Failed to decrypt with DPAPI` 时，请换用 Chrome / Firefox，
> 或导出一份 cookies.txt 在配置里指定 `cookies.cookies_file`。
>
> **关于浏览器正在运行**：Chrome/Edge 运行时会锁住 Cookie 数据库，
> 首次提取可能需要完全退出浏览器；成功提取后会缓存 7 天，之后不受影响。

### 录制 `dly record`

```bash
dly record https://live.bilibili.com/6                       # 录到断流/下播为止
dly record https://live.bilibili.com/6 --segment 30          # 每 30 分钟切一段
dly record https://live.douyin.com/123456789 -o D:/录制       # 指定保存目录
dly record https://live.bilibili.com/6 --format mp4          # 容器格式（默认 ts）
dly record https://live.bilibili.com/6 --fps 30              # 边录边转成 30 帧
```

立即开始录制单个直播间，实时显示已录时长与体积，Ctrl+C 立即停止（会先杀掉 ffmpeg 再退出，不留孤儿进程）。

> **关于录制帧率**：默认「原画直通」用 `-c copy` 纯转封装，CPU 几乎不占用。
> 指定 `--fps` 后会改用 libx264 边录边转码，CPU 占用明显升高，
> 配置一般的电脑建议保持默认；要转帧率也可以录完再用别的工具处理。

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

### 值守 `dly watch`

在 `config.yaml` 的 `record.rooms` 里每行填一个直播间链接（`#` 开头暂停监控），然后：

```bash
dly watch
```

开播自动录、下播自动停、断线自动续录，7×24 无人值守；每 60 秒播报一次录制进度。
Ctrl+C 会停掉所有正在进行的录制再退出。

## 🖥️ 桌面窗口

`dly gui`，或双击 `scripts/启动界面.bat`：

| 页签 | 能做什么 |
|---|---|
| 下载视频 | 粘贴链接/分享文案 → 选画质与帧率 → 开始下载，实时看进度 |
| 录制直播 | 填直播间链接 → 选帧率与录制时长（s / min / h）→ 录满自动停止；可勾选到点自动续录下一段，显示已录时长与体积 |
| 无人值守 | 填直播间列表 → 选帧率 → 后台值守，开播自动录 |

- 右上角切换 Cookie 来源（Chrome / Edge / Firefox / Brave）并一键测试提取结果
- 底部显示连接状态、ffmpeg 检测结果与版本号
- 界面只监听 `127.0.0.1`，不对局域网暴露；端口被占用会自动顺延到下一个
- **关窗口 = 退出程序**：先停录制/值守，再关本地服务，不留孤儿 ffmpeg 进程
- `dly gui --web` 改用系统浏览器打开（调试用），`dly gui --debug` 打开 DevTools

> 为什么界面是网页技术：Tkinter 依赖 tcl/tk，精简版 Python 常常不带；
> PyQt 要背上百 MB 运行时。用 pywebview 套一层系统自带的 WebView2，
> 既有原生窗口体验，界面代码又能直接复用到将来的手机端。

## 📦 打包成独立软件（免装 Python）

```bat
scripts\打包exe.bat
```

打包完成后产物在 `dist\DLY\`，把整个文件夹拷给别人，双击
`DLY.exe` 即可运行，**对方不需要装 Python**。

| 说明 | 内容 |
|---|---|
| 打包方式 | PyInstaller 文件夹模式（onedir），启动快、不触发杀软自解压告警 |
| 体积 | 约 260MB，其中 ffmpeg / ffprobe 占 190MB |
| 不带 ffmpeg | 设 `DLY_BUNDLE_FFMPEG=0` 再打包，程序会退回到系统 PATH 里的 ffmpeg |
| 指定 ffmpeg | 设 `DLY_FFMPEG` / `DLY_FFPROBE` 指向你要打包的版本 |
| 用户数据 | 日志与默认下载目录在 `%LOCALAPPDATA%\DLY\`（程序装在无写权限目录也能跑） |
| 配置 | 把 `config.yaml` 放在 exe 同目录即可生效 |

> 图标：`packaging/app.ico`（透明底，7 档尺寸）。换源图就改 `packaging/icon_source.jpg`
> 后重跑 `python packaging/make_icon.py`，它会同步生成 `app.ico`、`app.png` 和界面 `logo.png`。
> 打包后的 exe 带完整版本信息（右键属性可见），已关闭 UPX 压缩以免被误报。

打包后仍可用的启动参数：`DLY.exe --web`（用浏览器打开）、`--debug`（开 DevTools）。

### 桌面快捷方式

打包时会自动在桌面创建「帅羊羊DLY」快捷方式。想单独重建（比如换了安装位置）：

```bat
scripts\创建桌面快捷方式.bat
```

也可以指定目录或换个名字：

```bash
python packaging/make_shortcut.py --app-dir "D:/Apps/DLY" --name "视频下载器"
```

> 快捷方式走系统 `IShellLink` 接口生成，指向 exe 的绝对路径。
> 如果把 `dist\DLY\` 挪走或删掉，快捷方式就会失效，需要重新创建。

### 分发给别人时

1. 把 `dist\DLY\` 整个目录压缩成 zip 发出去（别只发 exe，会缺依赖）
2. 提醒对方首次使用前在浏览器登录 douyin.com，程序会自动提取 Cookie
3. 首次运行 Windows 可能弹 SmartScreen「未知发布者」——因为没买代码签名证书，
   点「更多信息 → 仍要运行」即可；这是所有未签名 exe 的通用现象

## ⚙️ 配置说明

复制 `config.example.yaml` 为 `config.yaml`，主要选项：

| 选项 | 说明 | 默认值 |
|---|---|---|
| `output_dir` | 保存根目录 | 源码运行 `./downloads`；打包后 `%LOCALAPPDATA%\DLY\downloads` |
| `quality` | 默认画质 | `best` |
| `fps` | 下载后转码的目标帧率（0 = 不转码） | `0` |
| `keep_original` | 转码后保留原片（另存 `_原画`） | `false` |
| `cookies.browser` | 从哪个浏览器提取 Cookie | `chrome` |
| `cookies.cookies_file` | 手动指定 cookies.txt（优先级最高） | 空 |
| `cookies.cache_days` | Cookie 本地缓存天数（0 关闭） | `7` |
| `proxy` | 网络代理 | 空 |
| `record.format` | 录制格式 | `ts` |
| `record.fps` | 录制帧率（0 = 原画直通不转码） | `0` |
| `record.poll_interval` | 开播检测间隔（秒） | `60` |
| `record.max_retries` | 单场断线重连上限 | `20` |
| `record.segment_minutes` | 按时长分段（0 不分段） | `0` |

## 🔒 隐私与安全

- Cookie 只在你本机内存、系统临时目录和 `~/.dly/cookie_cache.json` 中流转，**不会上传任何地方**
- 缓存可通过 `dly cache --clear` 随时清除
- `config.yaml` 与 `cookies*.txt` 已被 `.gitignore` 排除，不会误提交
- 请勿把含 Cookie 的文件发给任何人，那等同于你的账号登录态

## 🏗️ 项目结构

```
src/dly/
├── cli.py               # 命令行入口
├── core/                # 配置、日志、命名、ffmpeg、帧率转码、路径解析、跨平台兼容
│   ├── transcode.py     # 按目标帧率重编码（下载后转码 / 录制时转码共用）
│   └── paths.py         # 源码运行 / 打包运行 的路径差异收敛在这里
├── cookies/             # 浏览器 Cookie 自动提取
├── platforms/           # 平台适配层（douyin / generic 兜底）
│   └── douyin_sign.py   # 抖音 a_bogus 请求签名（纯 Python）
├── downloader/          # 下载引擎、图文下载
├── recorder/            # 直播监控与录制
└── gui/                 # 图形界面
    ├── desktop.py       # 桌面窗口（pywebview，复用系统 WebView2）
    ├── server.py        # 本地服务与 JSON 接口
    └── assets/index.html

packaging/
├── app_main.py          # exe 入口（异常兜底 + 写日志）
├── dly.spec   # PyInstaller 打包配置
├── make_icon.py         # 由 icon_source.jpg 生成 app.ico / app.png / logo.png
├── icon_source.jpg      # 去水印后的白底插画（抠图在 make_icon.py 里做）
├── make_shortcut.py     # 建桌面快捷方式
└── app.ico
```

## 🙏 致谢

- **[DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)**（MIT）：抖音直播接口的调用方式与
  `a_bogus` 签名算法参考自该项目，`platforms/douyin_sign.py` 即其实现的移植（已按 MIT 协议保留署名）
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)**：下载引擎与浏览器 Cookie 解密，覆盖 1000+ 站点
- **[FFmpeg](https://ffmpeg.org/)**：转封装、合并、录制与帧率转码

## ⚠️ 免责声明

本工具仅供**个人学习与技术研究**使用。请遵守各内容平台的服务协议与版权规定：

- 下载/录制的内容版权归原作者所有，请勿用于商业用途或未经授权的二次分发
- 请尊重创作者的劳动成果与隐私，不要录制、传播禁止录制的内容
- 使用本工具产生的一切法律后果由使用者本人承担，与项目作者无关

## 📄 License

[MIT](LICENSE)
