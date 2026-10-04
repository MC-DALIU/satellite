"""
依赖自检

整个文件只用标准库，所以在「什么都还没装」的情况下它自己也能跑起来，
把「缺哪些包、怎么装」直接告诉用户，而不是甩一个 ModuleNotFoundError 出来。

首次运行时如果发现缺依赖，会尝试自动 `pip install -r requirements.txt`
（不想自动装就设环境变量 FY4B_NO_AUTO_INSTALL=1）。
"""

import importlib.util
import os
import subprocess
import sys

#: (import 名, pip 包名)
CORE_MODULES = [
    ("PIL", "pillow"),
    ("filelock", "filelock"),
    ("apscheduler", "apscheduler"),
]
GUI_MODULES = CORE_MODULES + [("PyQt5", "PyQt5")]

#: 给 Arch 这类发行版用：pip 被 PEP 668 拦住时的等价包名
PACMAN_NAMES = {
    "pillow": "python-pillow",
    "filelock": "python-filelock",
    "apscheduler": "python-apscheduler",
    "PyQt5": "python-pyqt5",
}


def requirementsPath() -> str:
    """仓库根目录下的 requirements.txt"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "requirements.txt")


def missingModules(mods) -> list[tuple[str, str]]:
    return [(imp, pkg) for imp, pkg in mods if importlib.util.find_spec(imp) is None]


def installRequired(mods, only: list[tuple[str, str]] | None = None) -> bool:
    """调 pip 装依赖；优先用 requirements.txt，找不到就按包名装"""
    req = requirementsPath()
    cmd = [sys.executable, "-m", "pip", "install"]
    if os.path.isfile(req) and not only:
        cmd += ["-r", req]
    else:
        cmd += [pkg for _, pkg in (only or mods)]
    print("  执行：" + " ".join(cmd), flush=True)
    try:
        return subprocess.call(cmd) == 0
    except OSError as e:
        print(f"  调用 pip 失败：{e}", flush=True)
        return False


def require(mods, auto: bool = False, title: str = "FY4B") -> None:
    """
    检查依赖，缺了就提示（auto=True 时顺手装掉）

    装完还缺就直接退出，别让用户对着一长串 traceback 发愣。
    """
    missing = missingModules(mods)
    if not missing:
        return

    names = "、".join(f"{imp}（pip 包名 {pkg}）" for imp, pkg in missing)
    print(f"[{title}] 缺少依赖：{names}", flush=True)

    if auto and os.environ.get("FY4B_NO_AUTO_INSTALL") != "1":
        print(f"[{title}] 正在自动安装依赖，稍等…", flush=True)
        print(
            f"[{title}] （不想自动装的话，设 FY4B_NO_AUTO_INSTALL=1 再运行）", flush=True
        )
        installRequired(mods, only=missing)
        missing = missingModules(mods)
        if not missing:
            print(f"[{title}] 依赖已就绪，继续启动。", flush=True)
            return

    lines = [
        f"[{title}] 还有依赖没装好，请手动处理后重试：",
        f'    "{sys.executable}" -m pip install -r "{requirementsPath()}"',
    ]
    pacman = [PACMAN_NAMES[pkg] for _, pkg in missing if pkg in PACMAN_NAMES]
    if pacman:
        lines.append("  如果是 Arch 这类发行版（pip 会被 PEP 668 拦住），改用系统包管理器：")
        lines.append(f"      sudo pacman -S {' '.join(pacman)}")
    print("\n".join(lines), flush=True)
    sys.exit(1)
