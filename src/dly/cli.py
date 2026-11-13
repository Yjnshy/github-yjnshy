"""命令行入口：dly download / dly record / dly watch。"""

from __future__ import annotations

import logging
import signal

import click
from rich.console import Console

from dly import __version__
from dly.core.config import load_config
from dly.core.logging import setup_logging

console = Console()


def _enable_graceful_interrupts() -> None:
    """让 Ctrl+Break / kill 与 Ctrl+C 一样走优雅停止。

    Windows 的控制台里，Ctrl+Break 默认直接终止进程（不会抛
    KeyboardInterrupt），ffmpeg 子进程就会变成孤儿继续写文件。
    这里统一映射到默认的 Ctrl+C 处理，保证停止逻辑只有一条路。
    """
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        try:
            signal.signal(sigbreak, signal.default_int_handler)
        except (ValueError, OSError):
            pass


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="dly (dly)")
@click.option("--config", "config_path", default=None,
              help="配置文件路径（默认找 ./config.yaml 或 ~/.dly/config.yaml）")
@click.option("--verbose", is_flag=True, help="输出调试日志")
@click.option("--browser", default=None,
              type=click.Choice(["chrome", "edge", "firefox", "brave"]),
              help="临时指定提取 Cookie 的浏览器（覆盖配置文件）")
@click.pass_context
def main(ctx: click.Context, config_path: str | None, verbose: bool,
         browser: str | None) -> None:
    """dly：多平台无水印下载与直播自动录制工具。"""
    _enable_graceful_interrupts()
    setup_logging(logging.DEBUG if verbose else logging.INFO)
    config = load_config(config_path)
    if browser:
        config.cookies.browser = browser
    ctx.ensure_object(dict)
    ctx.obj["config"] = config


@main.command()
@click.argument("text", nargs=-1, required=True)
@click.option("-q", "--quality", default=None,
              type=click.Choice(["best", "1080", "720", "480", "audio", "mp3"]),
              help="画质（默认读配置文件）")
@click.option("-o", "--output", "output_dir", default=None, help="保存目录")
@click.option("--fps", "fps", default=None, type=int,
              help="下载后转码到指定帧率（默认原帧率不转码；转码较耗时）")
@click.option("--keep-original", is_flag=True,
              help="转码后保留原片（另存为 xxx_原画）")
@click.pass_context
def download(ctx: click.Context, text: tuple, quality: str | None,
             output_dir: str | None, fps: int | None,
             keep_original: bool) -> None:
    """下载视频/图文（无水印）。支持整段分享文案直接粘贴。

    \b
    示例：
      dly download "https://v.douyin.com/xxxxx/"
      dly download "7.94 复制打开抖音… https://v.douyin.com/xxxxx/ 12/25"
      dly download 链接1 链接2 -q 720 -o D:/视频
      dly download 链接 --fps 30            # 下载后转成 30 帧
    """
    from dly.core.transcode import normalize_fps
    from dly.downloader.engine import DownloadEngine

    config = ctx.obj["config"]
    if keep_original:
        config.keep_original = True
    engine = DownloadEngine(config,
                            progress=lambda msg: console.print(msg))
    success, total = engine.download_text(
        " ".join(text), quality=quality, output_dir=output_dir,
        fps=normalize_fps(fps) if fps is not None else None)
    if success == total:
        console.print(f"[green]全部完成：{success}/{total}[/green]")
    else:
        console.print(f"[yellow]完成 {success} 个，失败 {total - success} 个[/yellow]")
        raise SystemExit(1)


@main.command()
@click.argument("room_url")
@click.option("-o", "--output", "output_dir", default=None, help="保存目录")
@click.option("--segment", "segment_minutes", default=None, type=int,
              help="按时长分段（分钟，0 表示整段录制）")
@click.option("--format", "fmt", default=None,
              type=click.Choice(["ts", "mp4", "mkv", "flv"]),
              help="输出容器（默认 ts，断电也不损坏）")
@click.option("--fps", "fps", default=None, type=int,
              help="录制帧率（默认原画直通；指定后边录边转码，CPU 占用高）")
@click.pass_context
def record(ctx: click.Context, room_url: str, output_dir: str | None,
           segment_minutes: int | None, fmt: str | None,
           fps: int | None) -> None:
    """录制单个直播间（立即开始，下播或断流超限自动停止）。

    \b
    示例：
      dly record https://live.douyin.com/123456789
      dly record https://live.bilibili.com/6 --segment 30
      dly record https://live.bilibili.com/6 --fps 30
    """
    import time
    from pathlib import Path

    from dly.core.ffmpeg import find_ffmpeg
    from dly.core.transcode import normalize_fps
    from dly.cookies.manager import CookieManager
    from dly.platforms import find_adapter
    from dly.recorder.recorder import LiveRecorder, RecordingError

    config = ctx.obj["config"]
    cookies = CookieManager(config.cookies)
    adapter = find_adapter(room_url, cookies, proxy=config.proxy)
    if adapter is None:
        console.print("[red]无法识别的直播间链接[/red]")
        raise SystemExit(1)

    console.print("正在获取直播流…")
    live = adapter.get_live_info(room_url)
    if not live or not live.is_live or not live.stream_url:
        console.print("[yellow]当前未开播或无法获取流地址[/yellow]")
        raise SystemExit(1)

    ffmpeg = find_ffmpeg(config.ffmpeg_path)
    if not ffmpeg:
        console.print("[red]未找到 ffmpeg，无法录制[/red]")
        raise SystemExit(1)

    outdir = Path(output_dir or config.output_dir) / "直播录制"
    target_fps = normalize_fps(fps) if fps is not None else config.record.fps
    if target_fps:
        console.print(f"[yellow]目标帧率 {target_fps}fps：边录边转码，CPU 占用较高[/yellow]")
    console.print(f"[red]🔴 录制中[/red] {live.anchor} - {live.title}")
    console.print(f"[dim]输出目录：{outdir}　Ctrl+C 停止[/dim]")

    last = [time.monotonic()]

    def on_progress(seconds: float, size: int) -> None:
        now = time.monotonic()
        if now - last[0] < 15:
            return
        last[0] = now
        hh, rem = divmod(int(seconds), 3600)
        mm, ss = divmod(rem, 60)
        console.print(f"[dim]  ⏱ {hh:02d}:{mm:02d}:{ss:02d}　"
                      f"{size / 1024 / 1024:.1f} MB[/dim]")

    recorder = LiveRecorder(
        ffmpeg, outdir,
        fmt=fmt or config.record.format,
        segment_minutes=(config.record.segment_minutes
                         if segment_minutes is None else segment_minutes),
        fps=target_fps,
        on_progress=on_progress,
    )
    try:
        outfile = recorder.record_once(live)
        console.print(f"[green]✓ 已保存：{outfile}[/green]")
    except KeyboardInterrupt:
        console.print("\n[yellow]已停止录制[/yellow]")
        raise SystemExit(0)
    except RecordingError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        if exc.detail:
            console.print(f"[dim]{exc.detail}[/dim]")
        raise SystemExit(1)


@main.command()
@click.pass_context
def watch(ctx: click.Context) -> None:
    """无人值守模式：监控 config.yaml 里 record.rooms 的所有直播间。"""
    from dly.recorder.monitor import LiveMonitor

    config = ctx.obj["config"]
    try:
        monitor = LiveMonitor(config,
                              notify=lambda msg: console.print(msg))
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)
    monitor.watch(config.record.rooms)


@main.command()
@click.option("--clear", is_flag=True, help="清空本地 Cookie 缓存（Cookie 失效时使用）")
@click.pass_context
def cache(ctx: click.Context, clear: bool) -> None:
    """管理 Cookie 本地缓存。

    \b
    示例：
      dly cache --clear     # Cookie 失效、换账号后清空缓存
    """
    from dly.cookies.manager import CookieManager

    manager = CookieManager(ctx.obj["config"].cookies)
    if clear:
        manager.clear_cache()
        console.print(f"[green]已清空缓存：{CookieManager.CACHE_PATH}[/green]")
    else:
        exists = CookieManager.CACHE_PATH.is_file()
        console.print(f"缓存文件：{CookieManager.CACHE_PATH}"
                      f"（{'存在' if exists else '不存在'}）")


@main.command()
@click.option("--web", is_flag=True,
              help="用系统浏览器打开（默认是独立桌面窗口）")
@click.option("--debug", is_flag=True,
              help="开发者模式：窗口内可打开 DevTools")
def gui(web: bool, debug: bool) -> None:
    """打开图形界面（桌面窗口：下载 / 录制 / 值守）。"""
    try:
        from dly.gui import launch
    except ImportError as exc:
        console.print(f"[red]无法加载图形界面：{exc}[/red]")
        raise SystemExit(1)
    launch(debug=debug, web=web)


if __name__ == "__main__":
    main()
