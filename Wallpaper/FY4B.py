#! /bin/python
"""
风云四号 B 星云图壁纸

从国家卫星气象中心（NSMC）下载 FY4B 全圆盘真彩云图，按配置里的裁剪框剪出
一块，再交给桌面环境设为壁纸。

用法：
    python3 FY4B.py              常驻运行，按配置的分钟点自动更新
    python3 FY4B.py --once       更新一次后退出
    python3 FY4B.py --download   只下载原图
    python3 FY4B.py --gui        打开图形界面
"""

import argparse
import datetime
import glob
import json
import logging
import os
import platform
import signal
import subprocess
import sys
import threading
import time

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from filelock import FileLock, Timeout
from PIL import Image

HOME = os.environ.get("HOME") or os.path.expanduser("~")
downloadPath = os.path.join(HOME, ".cache", "fy4b") + os.sep
configDir = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
configPath = os.path.join(configDir, "fy4b", "config.json")
autostartPath = os.path.join(configDir, "autostart", "fy4b-wallpaper.desktop")
lockPath = os.path.join(downloadPath, "app.lock")
pidPath = os.path.join(downloadPath, "app.pid")
lock = FileLock(lockPath, timeout=0)

Image.MAX_IMAGE_PIXELS = 300000000  # 设置最大图片尺寸

# 配置文件里可调的项，GUI 也按这些键读写
DEFAULT_CONFIG = {
    "url": "http://img.nsmc.org.cn/CLOUDIMAGE/FY4B/AGRI/GCLR/FY4B_DISK_GCLR.JPG",
    "crop_x": 2400,
    "crop_y": 1200,
    "crop_w": 8640,
    "crop_h": 4860,
    "lock_aspect": True,
    "nudge_step": 10,
    "jpeg_quality": 90,
    "keep": 2,
    "wallpaper_backend": "auto",  # auto / KDE / niri
    "schedule_enabled": True,
    "schedule_minutes": "10,25,40,55",
    "startup_max_age": 20,  # 启动时若上次更新已超过这么多分钟就立刻更新一张
    "request_timeout": 60,
    "verify": True,
    # ---- 界面模式与后台行为 ----
    "mode": "full",  # full=完整模式（有窗口） / light=轻量模式（只在后台跑）
    "enable_tray": True,  # 轻量模式是否留一个托盘图标
    "autostart": False,  # 实际以 autostart 的 .desktop 文件是否存在为准
    # ---- 壁纸被手动更换的检测 ----
    "watch_wallpaper": True,  # 检测到别人换了壁纸就暂停轮换
    "watch_interval": 60,  # 检测间隔（秒）
    "rotation_paused": False,  # 轮换是否已暂停（跨进程共享，存在配置里）
    "paused_at": "",
    "last_applied": "",  # 最近一次成功设置的壁纸，没设过就不做替换检测
}

logger = logging.getLogger("Logging")


def initLog() -> logging.Logger:
    """
    初始化日志模块

    重复调用不会重复添加 handler，所以 GUI 里再调一次也没关系。
    """
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        checkDir(downloadPath)
        file_hander = logging.FileHandler(os.path.join(downloadPath, "fy4b.log"))
        file_hander.setLevel(logging.INFO)
        file_hander.setFormatter(formatter)
        logger.addHandler(file_hander)
    except OSError as e:
        print(f"日志文件不可用（{e}），只输出到终端")

    console_hander = logging.StreamHandler()
    console_hander.setLevel(logging.INFO)
    console_hander.setFormatter(formatter)
    logger.addHandler(console_hander)
    return logger


def checkDir(path: str) -> None:
    """检查目录是否创建，如果没有则创建"""
    os.makedirs(path, exist_ok=True)


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def loadConfig() -> dict:
    """读取配置，缺失的项用默认值补齐"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(configPath, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update({k: saved[k] for k in saved if k in DEFAULT_CONFIG})
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"配置文件读取失败，改用默认值：{e}")
    return cfg


def saveConfig(cfg: dict) -> None:
    """把配置写回磁盘（先写临时文件再替换，避免写一半损坏）"""
    data = {k: cfg.get(k, v) for k, v in DEFAULT_CONFIG.items()}
    checkDir(os.path.dirname(configPath))
    tmp = configPath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, configPath)
    logger.info(f"配置已保存：{configPath}")


def constrainBox(
    x: float,
    y: float,
    w: float,
    h: float,
    img_w: int,
    img_h: int,
    aspect: float | None = None,
    min_size: int = 32,
) -> tuple[int, int, int, int]:
    """
    把裁剪框限制在图片范围内，可选保持宽高比

    原代码写死的 2400 + 8640 已经超出图片右边界（宽 10992），超出部分会被
    PIL 补成黑边，所以这里统一做一次夹取。返回 (x, y, w, h)。
    """
    w = max(min_size, min(int(round(w)), img_w))
    h = max(min_size, min(int(round(h)), img_h))

    if aspect and aspect > 0:
        # 先按宽度推高度，放不下就反过来按高度推宽度
        h = int(round(w / aspect))
        if h > img_h:
            h = img_h
            w = int(round(h * aspect))
        if w > img_w:
            w = img_w
            h = int(round(w / aspect))
        w = max(min_size, min(w, img_w))
        h = max(min_size, min(h, img_h))

    x = max(0, min(int(round(x)), img_w - w))
    y = max(0, min(int(round(y)), img_h - h))
    return x, y, w, h


def cropBox(cfg: dict, img_w: int, img_h: int) -> tuple[int, int, int, int]:
    """按配置算出最终裁剪框（已夹进图片范围）"""
    return constrainBox(
        cfg["crop_x"],
        cfg["crop_y"],
        cfg["crop_w"],
        cfg["crop_h"],
        img_w,
        img_h,
    )


# --------------------------------------------------------------------------
# 下载与裁切
# --------------------------------------------------------------------------


def cleanStaleParts(max_age: float = 3600.0) -> None:
    """清掉被强杀（SIGKILL）留下的临时下载文件，免得白占几十 MB"""
    now = time.time()
    for stale in glob.glob(os.path.join(downloadPath, "raw.jpg.*.part")):
        try:
            if now - os.path.getmtime(stale) > max_age:
                os.remove(stale)
        except OSError:
            pass


def downloadWallpaper(cfg: dict | None = None) -> str:
    """
    下载壁纸原图

    和旧版相比：所有 requests 异常都会重试、检查 HTTP 状态码、先写临时文件
    再原子替换（避免中途失败留下半张图让人误用）。临时文件名带上 pid，这样
    常驻进程和图形界面同时下载也不会互相踩，raw.jpg 永远是完整的。
    """
    cfg = cfg or loadConfig()
    checkDir(downloadPath)
    cleanStaleParts()
    downloadFile = os.path.join(downloadPath, "raw.jpg")
    tmpFile = f"{downloadFile}.{os.getpid()}.part"
    url = cfg["url"]
    max_retry = 5
    last_error: Exception | None = None

    for attempt in range(1, max_retry + 1):
        try:
            logger.info(f"下载中（第 {attempt}/{max_retry} 次）：{url}")
            res = requests.get(url, timeout=cfg["request_timeout"])
            res.raise_for_status()
            with open(tmpFile, "wb") as f:
                f.write(res.content)
            os.replace(tmpFile, downloadFile)
            logger.info(
                f"下载成功：{len(res.content) / 1024 / 1024:.1f} MiB → {downloadFile}"
            )
            return downloadFile
        except requests.exceptions.RequestException as e:
            last_error = e
            logger.error(f"下载失败（第 {attempt}/{max_retry} 次）：{type(e).__name__}: {e}")
            if attempt < max_retry:
                time.sleep(3)

    try:
        os.remove(tmpFile)
    except OSError:
        pass
    raise RuntimeError(f"下载失败，已重试 {max_retry} 次：{last_error}")


def cropWallpaper(image_path: str, cfg: dict | None = None) -> str:
    """
    按配置裁剪图片

    输出文件名带上时间戳。KDE Plasma 的图像壁纸插件把配置项 Image 的取值
    既当成「壁纸有没有变」的判断依据，又当成图片缓存的 key：如果每次都写
    同一个路径，Plasma 会认为壁纸从未变化，于是只有第一次设置生效，之后
    的更新全部无效。每轮生成一个新的文件名，可以同时让配置值和图片缓存
    失效，从而让壁纸真正刷新。
    """
    cfg = cfg or loadConfig()
    with Image.open(image_path) as img:
        img_w, img_h = img.size
        x, y, w, h = cropBox(cfg, img_w, img_h)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        crop_path = os.path.join(downloadPath, f"end_{stamp}.jpg")
        region = img.crop((x, y, x + w, y + h))
        region.save(crop_path, "JPEG", quality=int(cfg["jpeg_quality"]))
    logger.info(
        f"裁剪完成：({x}, {y}) {w}×{h} → {os.path.basename(crop_path)}"
        f"（{os.path.getsize(crop_path) / 1024 / 1024:.1f} MiB）"
    )
    cleanOldWallpaper(int(cfg["keep"]))
    return crop_path


def cleanOldWallpaper(keep: int = 2) -> None:
    """
    清理旧的剪裁图，只保留最近生成的 keep 张

    为了绕开 Plasma 的图片缓存，每轮都会生成新的文件名，因此需要清理，
    否则 ~/.cache/fy4b/ 会无限增长。多留一张是为了让上一张壁纸在切换
    动画结束之前仍然存在于磁盘上。文件名带零填充的时间戳，所以按文件名
    倒序排列就等于按生成时间倒序排列，不需要额外 stat。
    """
    wallpapers = sorted(glob.glob(os.path.join(downloadPath, "end*.jpg")), reverse=True)
    for stale in wallpapers[keep:]:
        try:
            os.remove(stale)
            logger.debug(f"已清理旧壁纸：{os.path.basename(stale)}")
        except OSError as e:
            logger.warning(f"清理旧壁纸失败 {stale}：{e}")


# --------------------------------------------------------------------------
# 设置壁纸
# --------------------------------------------------------------------------


def detectBackend(cfg: dict | None = None) -> str:
    """决定用哪个后端设置壁纸：配置写死了就用配置的，否则看桌面环境"""
    cfg = cfg or loadConfig()
    backend = str(cfg.get("wallpaper_backend", "auto"))
    if backend != "auto":
        return backend

    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    names = [name.lower() for name in desktop.replace(":", ";").split(";") if name]
    if any("kde" in name for name in names):
        return "KDE"
    if any("niri" in name for name in names):
        return "niri"

    osName = platform.system()
    if osName == "Windows":
        return "dwm"
    return "unknown"


def isDaemonRunning() -> bool:
    """
    检测是否有别的实例正拿着文件锁（只用于界面提示，不做任何限制）

    图形界面和常驻进程是可以同时干活的：下载用带 pid 的临时文件 + 原子替换，
    裁切结果文件名各不相同，最多只是多下一份原图、壁纸被设置两次（后写的赢）。
    """
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return True
    except Exception:
        return False
    else:
        lock.release()
        return False


# --------------------------------------------------------------------------
# 常驻进程的启停（图形界面用）
# --------------------------------------------------------------------------


def _pidAlive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pidLooksLikeUs(pid: int) -> bool:
    """确认这个 pid 确实是在跑本程序，避免 pid 被复用后误杀别人的进程"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
    except OSError:
        return True  # 读不到 /proc 就只好信任 pid 文件
    return "FY4B.py" in cmdline


def _fuserPids() -> list[int]:
    """问操作系统谁正持有锁文件，返回原始 pid 列表"""
    try:
        res = subprocess.run(
            ["fuser", lockPath], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for token in (res.stdout + " " + res.stderr).replace(":", " ").split():
        if token.isdigit():
            pids.append(int(token))
    return pids


def lockHolderPid() -> int | None:
    """
    谁正拿着文件锁（不关心它是谁）

    图形界面用它判断"占着锁的是不是另一个本程序实例"，
    这是单实例判定的权威依据：锁只能有一个主人。
    """
    pid: int | None = None
    try:
        with open(pidPath, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        pid = None
    if pid and pid != os.getpid() and _pidAlive(pid):
        return pid

    for other in _fuserPids():
        if other != os.getpid() and _pidAlive(other):
            return other
    return None


def _pidFromLock() -> int | None:
    """
    兜底：找出确实是命令行守护进程（FY4B.py）的锁持有者

    比 pid 文件更权威（持有锁的那个进程就是常驻服务），也能认出没写过
    pid 文件的老版本常驻进程。
    """
    for pid in _fuserPids():
        if pid != os.getpid() and _pidAlive(pid) and _pidLooksLikeUs(pid):
            return pid
    return None


def daemonPid() -> int | None:
    """正在运行的常驻服务 pid，没有就返回 None"""
    pid: int | None = None
    try:
        with open(pidPath, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        pid = None

    if pid and pid != os.getpid() and _pidAlive(pid) and _pidLooksLikeUs(pid):
        return pid
    return _pidFromLock()


def writePidFile() -> None:
    """记录本进程的 pid，好让图形界面能找到并停掉它"""
    try:
        checkDir(downloadPath)
        with open(pidPath, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        logger.warning(f"pid 文件写入失败：{e}")


def removePidFile() -> None:
    """只删掉属于本进程的 pid 文件"""
    try:
        with open(pidPath, "r", encoding="utf-8") as f:
            if int(f.read().strip()) != os.getpid():
                return
    except (OSError, ValueError):
        return
    try:
        os.remove(pidPath)
    except OSError:
        pass


def startDaemon(extra_args: list[str] | None = None) -> int:
    """
    另起一个常驻进程，返回它的 pid

    用 start_new_session 让它脱离当前进程组，这样图形界面关掉、或者终端里
    Ctrl+C，都不会牵连到它。
    """
    checkDir(downloadPath)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FY4B.py")
    out = open(os.path.join(downloadPath, "daemon.out"), "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, script, *(extra_args or [])],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=os.path.dirname(script),
            start_new_session=True,
        )
    finally:
        out.close()
    logger.info(f"已启动常驻服务，pid {proc.pid}")
    return proc.pid


def stopDaemon(pid: int | None = None, timeout: float = 15.0) -> bool:
    """停掉常驻服务（先 SIGTERM，超时不走就 SIGKILL），返回是否已停止"""
    pid = pid or daemonPid()
    if not pid:
        return True

    try:
        os.kill(pid, signal.SIGTERM)
        logger.info(f"已向常驻服务 {pid} 发送 SIGTERM")
    except OSError as e:
        logger.warning(f"停止常驻服务失败：{e}")
        return True

    deadline = time.time() + timeout
    while time.time() < deadline and _pidAlive(pid):
        time.sleep(0.2)

    if _pidAlive(pid):
        logger.warning(f"常驻服务 {pid} 没有响应 SIGTERM，改用 SIGKILL")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.5)

    alive = _pidAlive(pid)
    if not alive:
        try:
            os.remove(pidPath)
        except OSError:
            pass
    return not alive


def getEnv() -> str:
    """获取系统环境，返回窗口管理器的信息（只用于日志）"""
    osName = platform.system()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP") or "unknown"
    logger.info(f"操作系统：{osName}, 窗口环境：{desktop}")
    return desktop


def setWallpaper_KDE(path: str) -> None:
    """
    KDE Plasma 修改壁纸
    用 dbus，解决 plasma-apply-wallpaperimage 自作聪明的问题

    注意：Plasma 只有在配置项 Image 的取值发生变化时才会重新加载壁纸，
    所以调用方必须保证每次传入的路径都不一样（见 cropWallpaper）。
    另外顺手把 PreviewImage 写成 "null"：壁纸插件在 PreviewImage 不等于
    "null" 时会优先显示这张预览图而不是 Image，写掉它可以避免配置里
    残留的旧预览图让新壁纸显示不出来。
    """
    import dbus
    from urllib.parse import quote

    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    abs_path = os.path.abspath(path)
    file_url = "file://" + quote(abs_path, safe="/:@")

    script = f"""
        var ds = desktops();
        for (var i = 0; i < ds.length; i++) {{
            var d = ds[i];
            d.wallpaperPlugin = 'org.kde.image';
            d.currentConfigGroup = ['Wallpaper', 'org.kde.image', 'General'];
            d.writeConfig('Image', '{file_url}');
            d.writeConfig('PreviewImage', 'null');
        }}
    """
    bus = dbus.SessionBus()
    plasma = bus.get_object('org.kde.plasmashell', '/PlasmaShell')
    plasma.evaluateScript(script, dbus_interface='org.kde.PlasmaShell')


def plasmaWallpaper(screen: int) -> str | None:
    """
    读回 Plasma 当前真正在用的壁纸路径

    org.kde.PlasmaShell.wallpaper 是从运行中的壁纸插件读的，不是读磁盘上的
    配置文件，所以它反映的是实时状态，不会因为配置没落盘而撒谎。
    """
    import dbus

    bus = dbus.SessionBus()
    plasma = bus.get_object('org.kde.plasmashell', '/PlasmaShell')
    info = plasma.wallpaper(dbus.UInt32(screen), dbus_interface='org.kde.PlasmaShell')
    image = info.get('Image')
    return str(image) if image else None


def verifyWallpaper(image_path: str, backend: str | None = None) -> bool:
    """
    自检：回读桌面环境当前在用的壁纸，和刚写入的路径比对，结果写进日志

    这样每轮更新之后 fy4b.log 里都会有一行明确的结论，不用去盯着云图看
    有没有变化。只有 KDE 有回读接口，其他后端直接跳过。
    """
    backend = backend or detectBackend()
    if backend != "KDE":
        return True

    try:
        from urllib.parse import quote, unquote

        expect = "file://" + quote(os.path.abspath(image_path), safe="/:@")
        screens: list[tuple[int, str]] = []
        for screen in range(16):
            current = plasmaWallpaper(screen)
            if not current:
                break
            screens.append((screen, current))

        if not screens:
            logger.warning("自检未通过：读不到 Plasma 的壁纸状态")
            return False

        mismatched = [(s, c) for s, c in screens if unquote(c) != unquote(expect)]
        if mismatched:
            for screen, current in mismatched:
                logger.warning(
                    f"自检未通过：screen {screen} 实际在用 "
                    f"{os.path.basename(unquote(current))}，"
                    f"本轮写入 {os.path.basename(unquote(expect))}"
                )
            return False

        logger.info(
            f"自检通过：{len(screens)} 块屏都已切到 {os.path.basename(unquote(expect))}"
        )
        return True
    except Exception as e:
        logger.warning(f"自检失败（{type(e).__name__}: {e}）")
        return False


def isOurWallpaper(url: str | None) -> bool:
    """判断这个壁纸路径是不是我们自己生成的那批文件"""
    if not url:
        return False
    from urllib.parse import unquote

    path = unquote(url[7:]) if url.startswith("file://") else url
    path = os.path.abspath(path)
    if os.path.dirname(path) != os.path.abspath(downloadPath):
        return False
    name = os.path.basename(path)
    return name.startswith("end") and name.endswith(".jpg")


def foreignWallpaperScreens() -> list[tuple[int, str]] | None:
    """
    列出壁纸被换成了「不是我们设的文件」的屏幕

    返回 None 表示读不到（不是 KDE，或者 plasmashell 没在跑），空列表表示都正常。
    """
    if detectBackend() != "KDE":
        return None
    result: list[tuple[int, str]] = []
    try:
        for screen in range(16):
            current = plasmaWallpaper(screen)
            if not current:
                break
            if not isOurWallpaper(current):
                result.append((screen, current))
    except Exception as e:
        logger.debug(f"读取壁纸状态失败：{type(e).__name__}: {e}")
        return None
    return result


def setPaused(paused: bool) -> dict:
    """暂停 / 恢复自动轮换（状态写在配置里，各模式、各进程共享）"""
    cfg = loadConfig()
    cfg["rotation_paused"] = bool(paused)
    cfg["paused_at"] = (
        "" if not paused else datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    saveConfig(cfg)
    logger.info("已暂停自动轮换" if paused else "已恢复自动轮换")
    return cfg


def recordApplied(path: str) -> None:
    """记住最近一次成功设置的壁纸，之后靠它判断有没有被人换掉"""
    try:
        cfg = loadConfig()
        cfg["last_applied"] = os.path.abspath(path)
        saveConfig(cfg)
    except OSError as e:
        logger.warning(f"记录最近壁纸失败：{e}")


def notify(title: str, body: str) -> bool:
    """发一条桌面通知（先走 freedesktop 通知服务，不行就退回 notify-send）"""
    try:
        import dbus

        bus = dbus.SessionBus()
        obj = bus.get_object(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications"
        )
        iface = dbus.Interface(obj, "org.freedesktop.Notifications")
        iface.Notify(
            "FY4B",
            dbus.UInt32(0),
            "preferences-desktop-wallpaper",
            title,
            body,
            dbus.Array([], signature="s"),
            dbus.Dictionary({}, signature="sv"),
            dbus.Int32(10000),
        )
        return True
    except Exception as e:
        logger.debug(f"dbus 通知失败（{type(e).__name__}: {e}），改用 notify-send")

    try:
        subprocess.run(
            ["notify-send", "-a", "FY4B", "-i", "preferences-desktop-wallpaper", title, body],
            check=False,
            timeout=10,
        )
        return True
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"桌面通知发不出去：{e}")
        return False


def checkWallpaperReplaced(cfg: dict | None = None) -> bool:
    """
    检测壁纸是不是被人手动换掉了

    是的话：把轮换暂停写进配置 + 发一条桌面通知，返回 True。
    还没成功设置过壁纸（last_applied 为空）时不检测，避免第一次运行就误报。
    """
    cfg = cfg or loadConfig()
    if not cfg.get("watch_wallpaper", True):
        return False
    if cfg.get("rotation_paused"):
        return False  # 已经暂停了，不重复通知
    if not cfg.get("last_applied"):
        return False  # 还没设过壁纸，谈不到"被换掉"

    screens = foreignWallpaperScreens()
    if not screens:
        return False

    from urllib.parse import unquote

    names = "、".join(
        os.path.basename(unquote(url)) or url for _, url in screens[:3]
    )
    where = "/".join(str(screen) for screen, _ in screens)
    logger.warning(f"检测到壁纸被手动更换（screen {where} → {names}），暂停自动轮换")

    cfg["rotation_paused"] = True
    cfg["paused_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        saveConfig(cfg)
    except OSError as e:
        logger.error(f"暂停状态写入失败：{e}")

    notify(
        "FY4B 已暂停自动轮换",
        f"检测到壁纸被手动更换（{names}）。\n"
        f"要恢复自动轮换，请打开 FY4B 界面点「恢复轮换」。",
    )
    return True


# --------------------------------------------------------------------------
# 开机自启动（XDG autostart）
# --------------------------------------------------------------------------


def _quoteExec(path: str) -> str:
    """按 Desktop Entry 规范用双引号包住路径"""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def isAutostartEnabled() -> bool:
    return os.path.isfile(autostartPath)


def setAutostart(enabled: bool) -> bool:
    """开关开机自启动；自启动时用轻量模式，不会在登录时弹窗口"""
    if enabled:
        gui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.py")
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=FY4B 壁纸\n"
            "Comment=风云四号 B 星云图壁纸（轻量模式，后台自动更新）\n"
            f"Exec={_quoteExec(sys.executable)} {_quoteExec(gui)} --light\n"
            "Icon=preferences-desktop-wallpaper\n"
            "Terminal=false\n"
            "StartupNotify=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        try:
            checkDir(os.path.dirname(autostartPath))
            with open(autostartPath, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"已开启开机自启动：{autostartPath}")
            return True
        except OSError as e:
            logger.error(f"写入自启动文件失败：{e}")
            return False

    try:
        os.remove(autostartPath)
        logger.info("已关闭开机自启动")
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.error(f"删除自启动文件失败：{e}")
        return False
    return True


def setWallpaper(
    image_path: str, backend: str | None = None, cfg: dict | None = None
) -> bool:
    """设置壁纸，返回是否成功"""
    cfg = cfg or loadConfig()
    backend = backend or detectBackend(cfg)

    if not os.path.isfile(image_path):
        raise FileNotFoundError(image_path)

    if backend == "niri":
        # WARN: 这个地方后面该封装成函数，比如 awww,hyprpaper 等等壁纸设定程序（wayland 的壁纸设定程序真多……）
        subprocess.run(
            ["awww", "img", "-a", "--transition-type=center", image_path],
            check=True,
        )
    elif backend == "KDE":
        setWallpaper_KDE(image_path)
    else:
        logger.error(
            f"还没开始写，或者错误：当前桌面环境 {backend} 不支持，"
            f"可以在配置里把 wallpaper_backend 设为 KDE 或 niri"
        )
        return False

    logger.info(f"壁纸已设置：{os.path.basename(image_path)}")
    recordApplied(image_path)
    if cfg.get("verify", True):
        verifyWallpaper(image_path, backend)
    return True


def update(cfg: dict | None = None, force: bool = False) -> str | None:
    """
    完整的一轮更新：下载 → 裁剪 → 设置壁纸 → 自检

    自动轮换被暂停时直接跳过并返回 None；用户手动点按钮时传 force=True。
    """
    cfg = cfg or loadConfig()
    if cfg.get("rotation_paused") and not force:
        logger.info("自动轮换已暂停（壁纸被手动换过），跳过本次更新")
        return None

    started = time.time()
    logger.info("========== 开始更新壁纸 ==========")

    image_path = downloadWallpaper(cfg)
    crop_path = cropWallpaper(image_path, cfg)
    backend = detectBackend(cfg)
    if not setWallpaper(crop_path, backend=backend, cfg=cfg):
        raise RuntimeError(f"当前桌面环境不支持设置壁纸：{backend}")

    logger.info(f"========== 更新完成，耗时 {time.time() - started:.1f} 秒 ==========")
    return crop_path


# --------------------------------------------------------------------------
# 调度
# --------------------------------------------------------------------------


def watch_wake(scheduler: BackgroundScheduler) -> None:
    """
    睡眠唤醒自动恢复 + 定期检查壁纸有没有被人换掉
    """
    last = datetime.datetime.now()
    last_check = time.monotonic()
    while True:
        time.sleep(5)
        now = datetime.datetime.now()
        time_diff = now - last  # 对比时间差
        if time_diff > datetime.timedelta(minutes=1):
            try:
                logger.info("挂起，执行时间复位")
                if not scheduler.running:
                    logger.warning("调度器未运行，重启调度器")
                    scheduler.start()
                job = scheduler.get_job("update_wallpaper")  # 获取任务
                if job:
                    job.modify(next_run_time=datetime.datetime.now())
                else:
                    logger.error("找不到任务")
            except Exception as e:
                logger.error(f"唤醒处理失败：{e}", exc_info=True)

        last = now

        # 定期看看壁纸还是不是我们设的那张
        interval = 60
        try:
            interval = max(10, int(loadConfig().get("watch_interval", 60)))
        except (TypeError, ValueError):
            pass
        if time.monotonic() - last_check >= interval:
            last_check = time.monotonic()
            try:
                checkWallpaperReplaced()
            except Exception as e:
                logger.debug(f"壁纸检查失败：{type(e).__name__}: {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="风云四号 B 星云图壁纸")
    parser.add_argument("--once", action="store_true", help="只更新一次后退出")
    parser.add_argument("--download", action="store_true", help="只下载原图")
    parser.add_argument("--gui", action="store_true", help="打开图形界面")
    args = parser.parse_args(argv)

    if args.gui:
        gui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.py")
        os.execv(sys.executable, [sys.executable, gui])

    initLog()
    checkDir(downloadPath)
    try:
        lock.acquire()
    except Timeout:
        print("已有实例在运行，退出本次进程。")
        return 1

    writePidFile()
    try:
        cfg = loadConfig()
        getEnv()
        logger.info(f"配置：{configPath}")
        logger.info(f"壁纸后端：{detectBackend(cfg)}")

        if args.download:
            downloadWallpaper(cfg)
            return 0

        if args.once or not cfg["schedule_enabled"]:
            update(cfg)
            return 0

        scheduler = BackgroundScheduler()  # 创建一个后台调度器
        minutes = str(cfg["schedule_minutes"])
        scheduler.add_job(
            update,
            "cron",
            minute=minutes,
            id="update_wallpaper",
        )  # 添加任务

        try:
            update(cfg)
        except Exception as e:
            logger.error(f"首次更新失败：{type(e).__name__}: {e}")

        # SIGTERM / Ctrl+C 都走优雅退出，好让图形界面能干净地停掉本进程
        stopping = threading.Event()

        def _onSignal(signum, _frame):
            logger.info(f"收到信号 {signum}，准备退出")
            stopping.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _onSignal)
            except (ValueError, OSError):
                pass

        threading.Thread(target=watch_wake, args=(scheduler,), daemon=True).start()  # 启动监听
        scheduler.start()  # 开启调度器
        logger.info(f"调度器已启动，更新分钟：{minutes}")

        try:
            while not stopping.is_set():
                time.sleep(0.5)
        finally:
            scheduler.shutdown(wait=True)
            logger.info("调度器已关闭")
        return 0
    finally:
        removePidFile()


initLog()

if __name__ == "__main__":
    sys.exit(main())
