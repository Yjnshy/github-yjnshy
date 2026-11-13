"""命令行入口：mtk download / mtk record / mtk watch。"""

from __future__ import annotations

import logging

import click
from rich.console import Console

from media_toolkit import __version__
from media_toolkit.core.config import load_config
from media_toolkit.core.logging import setup_logging

console = Console()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="media-toolkit (mtk)")
@click.option("--config", "config_path", default=None,
              help="配置文件路径（默认找 ./config.yaml 或 ~/.media-toolkit/config.yaml）")
@click.option("--verbose", is_flag=True, help="输出调试日志")
@click.option("--browser", default=None,
              type=click.Choice(["chrome", "edge", "firefox", "brave"]),
              help="临时指定提取 Cookie 的浏览器（覆盖配置文件）")
@click.pass_context
def main(ctx: click.Context, config_path: str | None, verbose: bool,
         browser: str | None) -> None:
    """media-toolkit：多平台无水印下载与直播自动录制工具。"""
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
@click.pass_context
def download(ctx: click.Context, text: tuple, quality: str | None,
             output_dir: str | None) -> None:
    """下载视频/图文（无水印）。支持整段分享文案直接粘贴。

    \b
    示例：
      mtk download "https://v.douyin.com/xxxxx/"
      mtk download "7.94 复制打开抖音… https://v.douyin.com/xxxxx/ 12/25"
      mtk download 链接1 链接2 -q 720 -o D:/视频
    """
    from media_toolkit.downloader.engine import DownloadEngine

    engine = DownloadEngine(ctx.obj["config"],
                            progress=lambda msg: console.print(msg))
    success, total = engine.download_text(
        " ".join(text), quality=quality, output_dir=output_dir)
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
@click.pass_context
def record(ctx: click.Context, room_url: str, output_dir: str | None,
           segment_minutes: int | None, fmt: str | None) -> None:
    """录制单个直播间（立即开始，下播或断流超限自动停止）。

    \b
    示例：
      mtk record https://live.douyin.com/123456789
      mtk record https://live.bilibili.com/6 --segment 30
    """
    import time
    from pathlib import Path

    from media_toolkit.core.ffmpeg import find_ffmpeg
    from media_toolkit.cookies.manager import CookieManager
    from media_toolkit.platforms import find_adapter
    from media_toolkit.recorder.recorder import LiveRecorder, RecordingError

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
    from media_toolkit.recorder.monitor import LiveMonitor

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
      mtk cache --clear     # Cookie 失效、换账号后清空缓存
    """
    from media_toolkit.cookies.manager import CookieManager

    manager = CookieManager(ctx.obj["config"].cookies)
    if clear:
        manager.clear_cache()
        console.print(f"[green]已清空缓存：{CookieManager.CACHE_PATH}[/green]")
    else:
        exists = CookieManager.CACHE_PATH.is_file()
        console.print(f"缓存文件：{CookieManager.CACHE_PATH}"
                      f"（{'存在' if exists else '不存在'}）")


if __name__ == "__main__":
    main()
