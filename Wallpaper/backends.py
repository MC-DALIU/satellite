"""
壁纸后端

每个后端只管三件事：判断自己该不该被选中、把图片设成壁纸、回读当前壁纸。

**要支持一个新桌面环境，只需要在这里加一个 Backend 子类，并在 BACKENDS
里注册一行**，核心逻辑（下载 / 裁切 / 自检 / 暂停轮换）完全不用动。

目前实现：

===========  ========================================  ==========
后端          做法                                      「被替换」检测
===========  ========================================  ==========
KDE          通过 D-Bus 调 plasmashell 的脚本接口       支持（真机验证）
niri         调 awww img                               暂不支持
Windows      SystemParametersInfoW + 注册表            支持（**未真机验证**）
===========  ========================================  ==========

预留的坑位：GNOME / XFCE / Cinnamon / MATE / sway / Hyprland / 通用 X11 …
都是「跑一条命令 + 可选回读」，照着 `NiriBackend` 抄一个类即可。
"""

import logging
import os
import platform
import shutil
import subprocess
from urllib.parse import quote

logger = logging.getLogger("Logging")


class Backend:
    """壁纸后端基类"""

    #: 配置里写这个名字来强制指定后端
    name = ""
    #: 界面上显示给人看
    label = ""
    #: 能不能可靠回读「当前壁纸」。False 时核心层会跳过"被手动更换"的检测
    readable = False

    def detect(self, desktops: list[str]) -> bool:
        """
        自动识别时判断该不该选它

        :param desktops: XDG_CURRENT_DESKTOP 拆出来的小写名字列表
        """
        return False

    def available(self) -> bool:
        """当前环境看起来能不能用（只影响提示，不影响是否尝试）"""
        return True

    def setWallpaper(self, path: str) -> None:
        """把 path 设为壁纸；失败请抛异常，核心层会记日志并返回失败"""
        raise NotImplementedError

    def currentWallpaper(self, screen: int = 0) -> str | None:
        """回读某块屏当前在用的壁纸；None 表示没有或者读不到"""
        return None


# --------------------------------------------------------------------------
# KDE Plasma
# --------------------------------------------------------------------------


class KDEBackend(Backend):
    name = "KDE"
    label = "KDE Plasma（D-Bus）"
    readable = True

    def detect(self, desktops: list[str]) -> bool:
        return any("kde" in d for d in desktops)

    def available(self) -> bool:
        return platform.system() == "Linux"

    def setWallpaper(self, path: str) -> None:
        """
        用 dbus，解决 plasma-apply-wallpaperimage 自作聪明的问题

        注意：Plasma 只有在配置项 Image 的取值发生变化时才会重新加载壁纸，
        所以调用方必须保证每次传入的路径都不一样（见 cropWallpaper）。
        另外顺手把 PreviewImage 写成 "null"：壁纸插件在 PreviewImage 不等于
        "null" 时会优先显示这张预览图而不是 Image，写掉它可以避免配置里
        残留的旧预览图让新壁纸显示不出来。
        """
        import dbus

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

    def currentWallpaper(self, screen: int = 0) -> str | None:
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


# --------------------------------------------------------------------------
# niri（以及任何用 awww 的 wlroots 合成器）
# --------------------------------------------------------------------------


class NiriBackend(Backend):
    name = "niri"
    label = "niri / awww"
    # awww query 的输出格式没在真机上确认过，先不拿它做「被替换」判断，
    # 免得格式一变就误报成"壁纸被人换了"
    readable = False

    def detect(self, desktops: list[str]) -> bool:
        return any("niri" in d for d in desktops)

    def available(self) -> bool:
        return platform.system() == "Linux" and shutil.which("awww") is not None

    def setWallpaper(self, path: str) -> None:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        subprocess.run(
            ["awww", "img", "-a", "--transition-type=center", path],
            check=True,
        )


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

_SPI_SETDESKWALLPAPER = 20
_SPIF_UPDATEINIFILE = 0x01
_SPIF_SENDWININICHANGE = 0x02

#: 注册表里的壁纸显示方式：10 = 填充，0 = 居中，2 = 拉伸，6 = 适应，22 = 跨屏
WALLPAPER_STYLE_FILL = "10"


def decodeTranscodedImageCache(raw) -> str:
    """
    从注册表 TranscodedImageCache 里抠出壁纸路径

    这个值是二进制：前面有一小段头部，后面是 UTF-16LE 的路径，以两个 0 字节
    结尾。微软没有公开这个格式，所以只能尽力而为，抠不出来就返回空串。
    """
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 4:
        return ""
    for offset in (24, 0):
        body = bytes(raw[offset:])
        for i in range(0, len(body) - 1, 2):
            if body[i] == 0 and body[i + 1] == 0:
                body = body[:i]
                break
        try:
            text = body.decode("utf-16-le", "ignore").strip("\x00").strip()
        except (UnicodeDecodeError, ValueError):
            continue
        if text and (":" in text or "\\" in text or "/" in text):
            return text
    return ""


class WindowsBackend(Backend):
    """
    Windows 壁纸后端

    ⚠️ 这份代码**没有在真实的 Windows 上验证过**（开发机是 Linux）。
    逻辑本身是标准的 SystemParametersInfoW 用法，但注册表回读
    （尤其 TranscodedImageCache 的二进制格式）属于尽力而为：
    读不出来时会返回 None，核心层会跳过"被手动更换"的检测，不会误报。
    """

    name = "Windows"
    label = "Windows（SystemParametersInfoW）"
    readable = True

    def detect(self, desktops: list[str]) -> bool:
        return platform.system() == "Windows"

    def available(self) -> bool:
        return platform.system() == "Windows"

    def setWallpaper(self, path: str) -> None:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        abs_path = os.path.abspath(path)

        # 先把显示方式设成「填充」：我们的裁切框本来就是按屏幕比例裁的，
        # 如果 Windows 用居中/拉伸显示，构图就毁了
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Control Panel\Desktop",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ, WALLPAPER_STYLE_FILL)
                winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, "0")
        except Exception as e:  # winreg 在非 Windows 上根本不存在
            logger.warning(f"设置 Windows 壁纸显示方式失败（不影响换图）：{e}")

        import ctypes

        user32 = ctypes.windll.user32
        user32.SystemParametersInfoW.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_wchar_p,
            ctypes.c_uint,
        ]
        user32.SystemParametersInfoW.restype = ctypes.c_bool
        ok = user32.SystemParametersInfoW(
            _SPI_SETDESKWALLPAPER,
            0,
            str(abs_path),
            _SPIF_UPDATEINIFILE | _SPIF_SENDWININICHANGE,
        )
        if not ok:
            raise OSError(
                f"SystemParametersInfoW 调用失败（GetLastError={ctypes.get_last_error()}）"
            )

    def currentWallpaper(self, screen: int = 0) -> str | None:
        if screen != 0:  # Windows 所有显示器共用一张壁纸
            return None
        try:
            import winreg
        except ImportError:
            return None

        value = ""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
                try:
                    value = str(winreg.QueryValueEx(key, "WallPaper")[0] or "")
                except FileNotFoundError:
                    value = ""
                if not value:
                    # Win10 之后 WallPaper 经常是空的，真正的路径在
                    # TranscodedImageCache 里
                    try:
                        raw = winreg.QueryValueEx(key, "TranscodedImageCache")[0]
                        value = decodeTranscodedImageCache(raw)
                    except FileNotFoundError:
                        value = ""
        except OSError as e:
            logger.debug(f"读取 Windows 壁纸注册表失败：{e}")
            return None

        if not value:
            return None
        return "file:///" + value.replace("\\", "/").lstrip("/")


# --------------------------------------------------------------------------
# 注册表：加新后端就在这里加一行
# --------------------------------------------------------------------------

BACKEND_ORDER = [
    "Windows",
    "KDE",
    "niri",
    # 预留：GNOME / XFCE / Cinnamon / MATE / sway / Hyprland / 通用 X11 …
]

BACKENDS: dict[str, Backend] = {
    b.name: b for b in (WindowsBackend(), KDEBackend(), NiriBackend())
}


def getBackend(name: str) -> Backend | None:
    return BACKENDS.get(name)


def backendChoices() -> list[str]:
    """界面下拉框用：auto + 所有已注册后端"""
    return ["auto"] + [n for n in BACKEND_ORDER if n in BACKENDS]


def detectBackendName(forced: str = "auto") -> str:
    """
    决定用哪个后端：配置写死了就用配置的，否则按环境自动识别

    自动识别按 BACKEND_ORDER 的顺序问每个后端，谁先认领就用谁。
    """
    if forced and forced != "auto":
        return forced

    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
    desktops = [part for part in desktop.replace(":", ";").split(";") if part]

    for name in BACKEND_ORDER:
        backend = BACKENDS.get(name)
        if backend is not None and backend.detect(desktops):
            return name
    return "unknown"
