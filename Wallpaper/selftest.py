#!/usr/bin/env python3
"""
自检脚本

不联网、不改壁纸、不碰真实配置，只把关键逻辑跑一遍，用来快速定位问题。
在出问题的机器上（比如 Windows）直接：

    python selftest.py

把输出发出来即可。

为什么要写这个：有一类 bug 只在**运行时**才暴露 —— 比如改代码时不小心删掉了
某个常量，正常启动一切正常，等几分钟后"壁纸替换检测"第一次触发才抛
NameError。所以这里既做静态检查，也把那几条容易踩坑的分支真的调用一遍。
"""

import ast
import builtins
import os
import platform
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backends
import deps
import FY4B

_results: list[tuple[str, bool, str]] = []


def check(name: str, got, want) -> None:
    ok = got == want
    _results.append((name, ok, f"got={got!r} want={want!r}"))
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + ("" if ok else f"   {got!r} != {want!r}"))


def checkTrue(name: str, cond, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {detail}" if not cond else ""))


# --------------------------------------------------------------------------


def checkUndefinedNames() -> None:
    """
    静态扫一遍所有模块，揪出「引用了但从来没被赋值」的名字

    就是这条检查能防住上面说的那种运行时才炸的坑。
    """
    print("\n== 静态检查：未定义的名字 ==")
    here = os.path.dirname(os.path.abspath(__file__))
    for fname in ("FY4B.py", "gui.py", "backends.py", "deps.py"):
        path = os.path.join(here, fname)
        if not os.path.isfile(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
                a = node.args
                for arg in list(a.args) + list(a.kwonlyargs) + list(getattr(a, "posonlyargs", [])):
                    defined.add(arg.arg)
                for arg in (a.vararg, a.kwarg):
                    if arg:
                        defined.add(arg.arg)
            elif isinstance(node, ast.ClassDef):
                defined.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for al in node.names:
                    defined.add((al.asname or al.name).split(".")[0])
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.Lambda):
                for arg in node.args.args:
                    defined.add(arg.arg)

        unknown = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                unknown.setdefault(node.id, node.lineno)
        bad = {k: v for k, v in unknown.items() if k not in defined}
        checkTrue(
            f"{fname} 没有未定义的名字",
            not bad,
            "、".join(f"L{v} {k}" for k, v in sorted(bad.items(), key=lambda kv: kv[1])),
        )


def checkPureFunctions() -> None:
    print("\n== 纯函数 ==")
    # 裁剪框夹取：原代码写死的 2400+8640 会超出 10992 宽的图
    check("constrainBox 右边界夹取", FY4B.constrainBox(2400, 1200, 8640, 4860, 10992, 11912),
          (2352, 1200, 8640, 4860))
    check("constrainBox 负坐标", FY4B.constrainBox(-100, -50, 500, 400, 1000, 1000), (0, 0, 500, 400))
    check("constrainBox 锁定 16:9", FY4B.constrainBox(0, 0, 4000, 100, 5000, 5000, 16 / 9)[2:],
          (4000, 2250))

    # 路径归一化：Linux / Windows / URL 编码都要能比
    check("归一化 Linux URL", FY4B.normalizeWallpaper("file:///home/x/.cache/fy4b/end_a.jpg"),
          "/home/x/.cache/fy4b/end_a.jpg")
    check("归一化 Windows 盘符", FY4B.normalizeWallpaper("file:///C:/Users/x/end_a.jpg"),
          "c:/users/x/end_a.jpg")
    check("归一化 反斜杠", FY4B.normalizeWallpaper("C:\\Users\\x\\end_a.jpg"), "c:/users/x/end_a.jpg")
    check("归一化 URL 编码", FY4B.normalizeWallpaper("file:///home/x/%E6%96%87%E6%A1%A3/a.jpg"),
          os.path.normpath("/home/x/文档/a.jpg").casefold())

    # 是不是我们自己设的壁纸
    real = FY4B.downloadPath
    try:
        FY4B.downloadPath = os.path.join(os.sep + "tmp", "fy4b") + os.sep
        ours = "file://" + FY4B.downloadPath + "end_1.jpg"
        check("识别自己的壁纸", FY4B.isOurWallpaper(ours), True)
        check("识别原图不是壁纸", FY4B.isOurWallpaper("file://" + FY4B.downloadPath + "raw.jpg"), False)
        check("识别别人的壁纸", FY4B.isOurWallpaper("file:///usr/share/wallpapers/a.jpg"), False)
        check("空值", FY4B.isOurWallpaper(None), False)
    finally:
        FY4B.downloadPath = real

    # Windows 注册表 Trans codedImageCache 解码
    win_path = r"C:\Users\x\AppData\Local\fy4b\end_1.jpg"
    raw = b"\x00" * 24 + win_path.encode("utf-16-le") + b"\x00\x00"
    check("TranscodedImageCache 解码", backends.decodeTranscodedImageCache(raw), win_path)
    check("TranscodedImageCache 垃圾数据", backends.decodeTranscodedImageCache(b"\xff\xfe\x01"), "")
    check("TranscodedImageCache 空值", backends.decodeTranscodedImageCache(None), "")

    # 平台路径
    check("Windows 平台路径",
          FY4B.platformPaths("Windows", {"APPDATA": "R", "LOCALAPPDATA": "L"}),
          (os.path.join("L", "fy4b"), os.path.join("R", "fy4b")))
    check("Linux 平台路径",
          FY4B.platformPaths("Linux", {"HOME": "/home/x", "XDG_CONFIG_HOME": "/cfg"}),
          ("/home/x/.cache/fy4b", os.path.join("/cfg", "fy4b")))


def checkBackends() -> None:
    print("\n== 后端注册表 ==")
    checkTrue("注册表非空", bool(backends.BACKENDS), str(list(backends.BACKENDS)))
    checkTrue("注册顺序里的都在表里",
              all(n in backends.BACKENDS for n in backends.BACKEND_ORDER))
    name = FY4B.detectBackend()
    checkTrue(f"自动识别出后端：{name}", name != "unknown", "识别失败，会不知道怎么设壁纸")
    for bname, impl in backends.BACKENDS.items():
        checkTrue(f"{bname} 后端可用性检查不抛异常", impl.available() in (True, False))
        try:
            impl.currentWallpaper(0)
            checkTrue(f"{bname} 回读不抛异常", True)
        except Exception as e:
            checkTrue(f"{bname} 回读不抛异常", False, f"{type(e).__name__}: {e}")


def checkWallpaperWatch() -> None:
    """
    替换检测：这条分支曾经因为常量被误删而每次都抛 NameError，
    而且只有等定时器跑起来才会暴露 —— 所以这里必须真的调用它。
    """
    print("\n== 壁纸替换检测（打桩，不动真实配置和壁纸） ==")
    real_foreign = FY4B.foreignWallpaperScreens
    real_save = FY4B.saveConfig
    real_notify = FY4B.notify
    real_strikes = FY4B._foreignStrikes
    try:
        FY4B.foreignWallpaperScreens = lambda *a, **k: [(0, "file:///tmp/someone-elses.jpg")]
        FY4B.saveConfig = lambda cfg: None
        FY4B.notify = lambda *a, **k: True
        FY4B._foreignStrikes = 0

        cfg = {"watch_wallpaper": True, "rotation_paused": False, "last_applied": "/tmp/ours.jpg"}
        first = FY4B.checkWallpaperReplaced(cfg)
        check("第 1 次读到别人的壁纸：先不暂停", first, False)
        second = FY4B.checkWallpaperReplaced(cfg)
        check("第 2 次确认后才暂停", second, True)
        check("已暂停后不再重复触发", FY4B.checkWallpaperReplaced(cfg), False)

        # 正常情况：壁纸还是我们的 → 不暂停，且计数清零
        FY4B.foreignWallpaperScreens = lambda *a, **k: []
        cfg2 = {"watch_wallpaper": True, "rotation_paused": False, "last_applied": "/tmp/ours.jpg"}
        check("壁纸正常时不暂停", FY4B.checkWallpaperReplaced(cfg2), False)
        check("计数已清零", FY4B._foreignStrikes, 0)

        # 关掉检测 / 已暂停 / 还没设过壁纸 → 都应该直接跳过
        check("关掉检测后跳过",
              FY4B.checkWallpaperReplaced({"watch_wallpaper": False}), False)
        check("还没设过壁纸时跳过",
              FY4B.checkWallpaperReplaced({"watch_wallpaper": True, "rotation_paused": False,
                                           "last_applied": ""}), False)
    except Exception as e:
        checkTrue("替换检测不抛异常", False, f"{type(e).__name__}: {e}")
    finally:
        FY4B.foreignWallpaperScreens = real_foreign
        FY4B.saveConfig = real_save
        FY4B.notify = real_notify
        FY4B._foreignStrikes = real_strikes


def checkReadback() -> None:
    print("\n== 回读与自检（只读） ==")
    impl = backends.getBackend(FY4B.detectBackend())
    if impl is None or not impl.readable:
        print("  当前后端不支持回读，跳过")
        return
    screens = FY4B.currentWallpapers()
    if not screens:
        print("  读不到当前壁纸（壁纸服务没在跑？），跳过")
        return

    current = screens[0][1]
    ours = FY4B.isOurWallpaper(current)
    foreign = FY4B.foreignWallpaperScreens() or []
    print(f"  当前壁纸    : {current}")
    print(f"  是我们设的吗: {ours}")
    print(f"  判为被替换  : {bool(foreign)}")
    # 这两个函数必须互相一致，否则检测会一会儿说是一会儿说不是
    checkTrue(
        "isOurWallpaper 与 foreignWallpaperScreens 结论一致",
        ours == (not foreign),
        f"isOurWallpaper={ours} 但 foreignWallpaperScreens={foreign}",
    )
    if ours:
        # 我们自己设的壁纸必须能被自检认出来
        checkTrue(
            "自检能认出自己设的壁纸",
            FY4B.verifyWallpaper(FY4B.normalizeWallpaper(current)),
            "回读值和写入值对不上，说明路径比对有问题",
        )
    else:
        print("  （当前壁纸不是本程序设的，跳过自检的正向用例）")


def main() -> int:
    print("=" * 62)
    print("FY4B 自检")
    print("=" * 62)
    print(f"系统      : {platform.system()} {platform.release()}")
    print(f"Python    : {sys.version.split()[0]}  ({sys.executable})")
    print(f"缺失依赖  : {deps.missingModules(deps.GUI_MODULES) or '无'}")
    print(f"自动后端  : {FY4B.detectBackend()}")
    print(f"可选后端  : {backends.backendChoices()}")
    print(f"数据目录  : {FY4B.downloadPath}")
    print(f"配置      : {FY4B.configPath}")

    checkUndefinedNames()
    checkPureFunctions()
    checkBackends()
    checkWallpaperWatch()
    checkReadback()

    failed = [name for name, ok, _ in _results if not ok]
    print("\n" + "=" * 62)
    if failed:
        print(f"结果：{len(_results) - len(failed)}/{len(_results)} 通过，失败 {len(failed)} 项：")
        for name in failed:
            print(f"  ✗ {name}")
        return 1
    print(f"结果：全部 {len(_results)} 项通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
