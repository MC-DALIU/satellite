#!/usr/bin/env python3
"""
FY4B 壁纸设置界面

左边是原图 + 裁切框预览，右边是基本设置。裁切框可以直接拖动、拖八个手柄
缩放，也可以用方向键微调。界面只负责改配置和手动触发一次更新，定时更新
仍然由 FY4B.py 常驻进程负责。
"""

import os
import sys
import time

from PyQt5.QtCore import (
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QFont,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)
from apscheduler.schedulers.background import BackgroundScheduler
from filelock import Timeout
from PIL import Image

import FY4B

# 单实例用的本地 socket 名字（第二次启动会通过它把已有窗口叫出来）
SERVER_NAME = "fy4b-wallpaper"

# 常用宽高比，下拉框里显示的名字 → 数值
ASPECTS = [    ("16:9", 16 / 9),
    ("16:10", 16 / 10),
    ("21:9", 21 / 9),
    ("4:3", 4 / 3),
    ("1:1", 1.0),
]


def pilToQImage(im: Image.Image) -> QImage:
    """PIL 图像转 QImage（必须 copy，否则底层 buffer 释放后画面会花）"""
    im = im.convert("RGB")
    data = im.tobytes("raw", "RGB")
    qim = QImage(data, im.width, im.height, im.width * 3, QImage.Format_RGB888)
    return qim.copy()


class CropView(QWidget):
    """显示原图，并在上面画一个可拖动 / 可缩放的裁切框"""

    OUTSIDE, TL, T, TR, R, BR, B, BL, L, INSIDE = range(10)

    boxChanged = pyqtSignal(int, int, int, int)

    MARGIN = 8
    HANDLE = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

        self._image: QImage | None = None
        self._srcSize = QSize()
        self._box = QRect()
        self._scale = 0.0
        self._origin = QPoint(0, 0)

        self._lockAspect = False
        self._aspect = 16 / 9

        self._dragMode = self.OUTSIDE
        self._dragStart = QPoint()
        self._boxAtStart = QRect()
        self._step = 10

    # ---------------- 数据 ----------------

    def loadImage(self, path: str, maxSide: int = 1500) -> QSize:
        """读入原图，只保留一份缩略图用于显示（原图动辄一亿像素）"""
        with Image.open(path) as im:
            src_w, src_h = im.size
            im.draft("RGB", (maxSide, maxSide))  # JPEG 可以按 1/2、1/4、1/8 快速解码
            im = im.convert("RGB")
            im.thumbnail((maxSide, maxSide), Image.LANCZOS)
            self._image = pilToQImage(im)
        self._srcSize = QSize(src_w, src_h)
        self._recalcLayout()
        self.update()
        return QSize(src_w, src_h)

    def clearImage(self) -> None:
        self._image = None
        self._srcSize = QSize()
        self._box = QRect()
        self.update()

    def sourceSize(self) -> QSize:
        return QSize(self._srcSize)

    def box(self) -> QRect:
        return QRect(self._box)

    def setBox(self, box: QRect, notify: bool = True) -> None:
        if self._srcSize.isEmpty():
            return
        aspect = self._aspect if self._lockAspect else None
        x, y, w, h = FY4B.constrainBox(
            box.x(), box.y(), box.width(), box.height(),
            self._srcSize.width(), self._srcSize.height(), aspect,
        )
        newBox = QRect(x, y, w, h)
        changed = newBox != self._box
        self._box = newBox
        self.update()
        if changed and notify:
            self.boxChanged.emit(x, y, w, h)

    def moveBy(self, dx: int, dy: int) -> None:
        self.setBox(self._box.translated(dx, dy))

    def growBy(self, dw: int, dh: int) -> None:
        """以中心为锚点缩放裁切框（键盘调整大小用）"""
        b = self._box
        self.setBox(
            QRect(
                b.x() - dw // 2,
                b.y() - dh // 2,
                b.width() + dw,
                b.height() + dh,
            )
        )

    def setLockAspect(self, locked: bool) -> None:
        self._lockAspect = bool(locked)

    def setAspect(self, aspect: float) -> None:
        if aspect and aspect > 0:
            self._aspect = float(aspect)

    def aspect(self) -> float:
        return self._aspect

    def cropPixmap(self) -> QPixmap | None:
        """当前裁切框在缩略图上的那一块，用作实时结果预览"""
        if self._image is None or self._srcSize.isEmpty() or self._box.isEmpty():
            return None
        k = self._image.width() / self._srcSize.width()
        r = QRect(
            int(self._box.x() * k),
            int(self._box.y() * k),
            max(1, int(self._box.width() * k)),
            max(1, int(self._box.height() * k)),
        )
        r = r.intersected(QRect(0, 0, self._image.width(), self._image.height()))
        if r.isEmpty():
            return None
        return QPixmap.fromImage(self._image.copy(r))

    # ---------------- 坐标换算 ----------------

    def _recalcLayout(self) -> None:
        if self._image is None or self._srcSize.isEmpty():
            return
        avail_w = max(1, self.width() - 2 * self.MARGIN)
        avail_h = max(1, self.height() - 2 * self.MARGIN)
        self._scale = min(
            avail_w / self._srcSize.width(), avail_h / self._srcSize.height()
        )
        draw_w = self._srcSize.width() * self._scale
        draw_h = self._srcSize.height() * self._scale
        self._origin = QPoint(
            int((self.width() - draw_w) / 2), int((self.height() - draw_h) / 2)
        )

    def _imageRect(self) -> QRect:
        if self._image is None:
            return QRect()
        return QRect(
            self._origin,
            QSize(
                int(self._srcSize.width() * self._scale),
                int(self._srcSize.height() * self._scale),
            ),
        )

    def _toView(self, x: float, y: float) -> QPoint:
        return QPoint(
            int(round(self._origin.x() + x * self._scale)),
            int(round(self._origin.y() + y * self._scale)),
        )

    def _toSrc(self, pt: QPoint) -> QPoint:
        if self._scale <= 0:
            return QPoint(0, 0)
        return QPoint(
            int(round((pt.x() - self._origin.x()) / self._scale)),
            int(round((pt.y() - self._origin.y()) / self._scale)),
        )

    def _boxToView(self) -> QRect:
        tl = self._toView(self._box.x(), self._box.y())
        br = self._toView(
            self._box.x() + self._box.width(), self._box.y() + self._box.height()
        )
        return QRect(tl, br)

    def _handleRects(self) -> dict:
        b = self._boxToView()
        s = self.HANDLE
        half = s // 2
        cx, cy = b.center().x(), b.center().y()
        anchors = {
            self.TL: (b.left(), b.top()),
            self.T: (cx, b.top()),
            self.TR: (b.right(), b.top()),
            self.R: (b.right(), cy),
            self.BR: (b.right(), b.bottom()),
            self.B: (cx, b.bottom()),
            self.BL: (b.left(), b.bottom()),
            self.L: (b.left(), cy),
        }
        return {k: QRect(v[0] - half, v[1] - half, s, s) for k, v in anchors.items()}

    def _hitTest(self, pos: QPoint) -> int:
        for mode, rect in self._handleRects().items():
            if rect.contains(pos):
                return mode
        if self._boxToView().contains(pos):
            return self.INSIDE
        return self.OUTSIDE

    # ---------------- 绘制 ----------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            self._paint(painter)
        finally:
            # 一定要 end()：如果画到一半抛异常，Qt 会因为 painter 没结束而 abort 整个进程
            painter.end()

    def _paint(self, p: QPainter) -> None:
        p.fillRect(self.rect(), QColor(28, 28, 32))

        if self._image is None:
            p.setPen(QColor(150, 150, 160))
            p.drawText(self.rect(), Qt.AlignCenter, "还没有原图\n点右侧的「下载原图」")
            return

        target = self._imageRect()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(target, self._image)
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)

        view_box = self._boxToView()
        if view_box.isEmpty():
            return

        # 框外压暗
        path = QPainterPath()
        path.addRect(QRectF(self.rect()))
        path.addRect(QRectF(view_box))
        path.setFillRule(Qt.OddEvenFill)
        p.fillPath(path, QColor(0, 0, 0, 120))

        # 三分线
        p.setPen(QPen(QColor(255, 255, 255, 55), 1))
        for i in (1, 2):
            x = view_box.left() + view_box.width() * i / 3
            y = view_box.top() + view_box.height() * i / 3
            p.drawLine(QPointF(x, view_box.top()), QPointF(x, view_box.bottom()))
            p.drawLine(QPointF(view_box.left(), y), QPointF(view_box.right(), y))

        # 边框
        p.setPen(QPen(QColor(96, 178, 255), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(view_box)

        # 手柄
        p.setPen(QPen(QColor(12, 40, 66), 1))
        p.setBrush(QColor(96, 178, 255))
        for rect in self._handleRects().values():
            p.drawRect(rect)

        # 尺寸标签
        label = (
            f"{self._box.width()}×{self._box.height()}  "
            f"@ ({self._box.x()}, {self._box.y()})"
        )
        p.setFont(QFont(self.font().family(), 9))
        metrics = p.fontMetrics()
        text_w = metrics.horizontalAdvance(label) + 12
        text_h = metrics.height() + 4
        tx = view_box.left() + 6
        ty = view_box.top() + 6
        if ty + text_h > view_box.bottom():
            ty = view_box.top() - text_h - 4
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(QRectF(tx, ty, text_w, text_h), 3, 3)
        p.setPen(QColor(235, 240, 250))
        p.drawText(QRectF(tx, ty, text_w, text_h), Qt.AlignCenter, label)

    def resizeEvent(self, event) -> None:
        self._recalcLayout()
        super().resizeEvent(event)

    # ---------------- 鼠标 ----------------

    def _cursorFor(self, mode: int):
        return {
            self.TL: Qt.SizeFDiagCursor,
            self.BR: Qt.SizeFDiagCursor,
            self.TR: Qt.SizeBDiagCursor,
            self.BL: Qt.SizeBDiagCursor,
            self.T: Qt.SizeVerCursor,
            self.B: Qt.SizeVerCursor,
            self.L: Qt.SizeHorCursor,
            self.R: Qt.SizeHorCursor,
            self.INSIDE: Qt.SizeAllCursor,
        }.get(mode, Qt.CrossCursor)

    def mousePressEvent(self, event) -> None:
        if self._image is None or event.button() != Qt.LeftButton:
            return
        self.setFocus()
        mode = self._hitTest(event.pos())
        if mode == self.OUTSIDE:
            return
        self._dragMode = mode
        self._dragStart = self._toSrc(event.pos())
        self._boxAtStart = QRect(self._box)

    def mouseMoveEvent(self, event) -> None:
        if self._image is None:
            return
        if self._dragMode == self.OUTSIDE:
            self.setCursor(self._cursorFor(self._hitTest(event.pos())))
            return

        sp = self._toSrc(event.pos())
        if self._dragMode == self.INSIDE:
            dx = sp.x() - self._dragStart.x()
            dy = sp.y() - self._dragStart.y()
            moved = self._boxAtStart.translated(dx, dy)
            x = max(0, min(moved.x(), self._srcSize.width() - moved.width()))
            y = max(0, min(moved.y(), self._srcSize.height() - moved.height()))
            self.setBox(QRect(x, y, moved.width(), moved.height()))
        else:
            self.setBox(self._resizeBox(self._dragMode, sp))
        self.setCursor(self._cursorFor(self._dragMode))

    def mouseReleaseEvent(self, event) -> None:
        self._dragMode = self.OUTSIDE
        self.setCursor(self._cursorFor(self._hitTest(event.pos())))

    def _resizeBox(self, mode: int, sp: QPoint) -> QRect:
        b = self._boxAtStart
        left, top, right, bottom = b.left(), b.top(), b.right(), b.bottom()
        if mode in (self.TL, self.L, self.BL):
            left = sp.x()
        if mode in (self.TR, self.R, self.BR):
            right = sp.x()
        if mode in (self.TL, self.T, self.TR):
            top = sp.y()
        if mode in (self.BL, self.B, self.BR):
            bottom = sp.y()
        if left > right:
            left, right = right, left
        if top > bottom:
            bottom, top = top, bottom

        aspect = self._aspect if self._lockAspect else None
        x, y, w, h = FY4B.constrainBox(
            left, top, right - left + 1, bottom - top + 1,
            self._srcSize.width(), self._srcSize.height(), aspect,
        )

        # 重新贴回没被拖动的那条边，让手柄跟手
        if mode in (self.TL, self.L, self.BL):
            x = b.right() - w + 1
        else:
            x = b.left()
        if mode in (self.TL, self.T, self.TR):
            y = b.bottom() - h + 1
        else:
            y = b.top()
        x = max(0, min(x, self._srcSize.width() - w))
        y = max(0, min(y, self._srcSize.height() - h))
        return QRect(x, y, w, h)

    # ---------------- 键盘 ----------------

    def keyPressEvent(self, event) -> None:
        if self._image is None:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key not in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            super().keyPressEvent(event)
            return

        step = max(1, self._step)
        if event.modifiers() & Qt.ShiftModifier:
            step *= 10

        dx = -step if key == Qt.Key_Left else (step if key == Qt.Key_Right else 0)
        dy = -step if key == Qt.Key_Up else (step if key == Qt.Key_Down else 0)

        if event.modifiers() & Qt.ControlModifier:
            # Ctrl + 方向键：缩放裁切框
            self.growBy(dx * 2, dy * 2)
        else:
            self.moveBy(dx, dy)

    def setStep(self, step: int) -> None:
        self._step = max(1, int(step))


class SchedulerBridge(QObject):
    """定时任务跑在 APScheduler 的线程里，只能靠信号回到界面线程"""

    started = pyqtSignal()
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)


class Worker(QThread):
    """把下载 / 裁切 / 设壁纸放到后台线程，避免界面卡死"""

    progressed = pyqtSignal(str)
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, action: str, cfg: dict, parent=None):
        super().__init__(parent)
        self.action = action
        self.cfg = cfg

    def run(self) -> None:
        try:
            if FY4B.isDaemonRunning():
                # 常驻服务在跑也不影响手动操作，两边同时干活是安全的
                self.progressed.emit("常驻服务也在运行，本次手动操作照常执行…")

            if self.action == "download":
                path = FY4B.downloadWallpaper(self.cfg)
                self.succeeded.emit(f"原图已下载：{os.path.basename(path)}")

            elif self.action == "crop":
                raw = os.path.join(FY4B.downloadPath, "raw.jpg")
                if not os.path.isfile(raw):
                    self.failed.emit("还没有原图，请先点「下载原图」。")
                    return
                path = FY4B.cropWallpaper(raw, self.cfg)
                if not FY4B.setWallpaper(path, cfg=self.cfg):
                    self.failed.emit("设置壁纸失败：当前桌面环境不被支持。")
                    return
                self.succeeded.emit(f"已裁切并应用：{os.path.basename(path)}")

            elif self.action == "update":
                # 用户手动点的，轮换暂停时也照做
                path = FY4B.update(self.cfg, force=True)
                if path is None:
                    self.succeeded.emit("已跳过本次更新（轮换处于暂停状态）")
                    return
                self.succeeded.emit(f"完整更新完成：{os.path.basename(path)}")

            else:
                self.failed.emit(f"未知操作：{self.action}")
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    def __init__(self, mode: str | None = None):
        super().__init__()
        self.setWindowTitle("FY4B 壁纸设置")
        self.resize(1280, 880)

        self.cfg = FY4B.loadConfig()
        self.worker: Worker | None = None
        self._loading = False

        # 调度器由本进程独占：靠文件锁保证不和别的 FY4B 进程重复跑
        self.scheduler: BackgroundScheduler | None = None
        self._lockHeld = False
        self.bridge = SchedulerBridge()

        # 托盘是可选件：轻量模式可以连托盘都没有
        self.tray: QSystemTrayIcon | None = None
        self.trayModeFull: QAction | None = None
        self.trayModeLight: QAction | None = None
        self.trayPauseAction: QAction | None = None
        self.trayResumeAction: QAction | None = None
        self.trayToggleAction: QAction | None = None
        self._trayNotified = False

        self.server: QLocalServer | None = None
        self.ipcListening = False
        self._foreignInstance = False
        self.mode = mode or str(self.cfg.get("mode", "full"))
        self._updateRunning = False
        self._watcher: QTimer | None = None

        self._buildUi()
        self._bindSignals()
        self._loadConfigToUi()
        self._loadPreview()
        self._refreshStatus()

        # 尽早占住单实例名字：越早，同时启动时的竞态窗口越小
        self.startIpcServer(clean=False)
        self._setupTray()
        self._takeOverScheduling()  # 拿文件锁并按配置起调度
        self._applyMode(self.mode, initial=True)
        self._startWatcher()
        QTimer.singleShot(2000, self._firstWallpaperCheck)

    # ---------------- 界面搭建 ----------------

    def _buildUi(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(10)

        # ---- 左：预览 ----
        left = QVBoxLayout()
        left.setSpacing(8)

        bar = QHBoxLayout()
        self.btnDownload = QPushButton("下载原图")
        self.btnCrop = QPushButton("裁切并应用")
        self.btnUpdate = QPushButton("完整更新")
        self.btnSave = QPushButton("保存设置")
        self.btnReset = QPushButton("还原默认")
        self.btnCrop.setToolTip("用已经下载好的原图按当前裁切框裁一次，并立即设为壁纸")
        self.btnUpdate.setToolTip("重新下载原图 → 裁切 → 设为壁纸（完整一轮）")
        for btn in (self.btnDownload, self.btnCrop, self.btnUpdate):
            btn.setMinimumHeight(32)
        for btn in (self.btnSave, self.btnReset):
            btn.setMinimumHeight(32)
        bar.addWidget(self.btnDownload)
        bar.addWidget(self.btnCrop)
        bar.addWidget(self.btnUpdate)
        bar.addStretch(1)
        bar.addWidget(self.btnSave)
        bar.addWidget(self.btnReset)
        left.addLayout(bar)

        # 结果横幅：只靠状态栏那行小字太容易漏看
        self.resultBanner = QLabel()
        self.resultBanner.setWordWrap(True)
        left.addWidget(self.resultBanner)
        self._setBanner("就绪。改完设置记得点「保存设置」，或直接点「裁切并应用」。")

        self.cropView = CropView()
        left.addWidget(self.cropView, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)

        self.resultPreview = QLabel("裁切结果")
        self.resultPreview.setFixedSize(320, 180)
        self.resultPreview.setAlignment(Qt.AlignCenter)
        self.resultPreview.setStyleSheet(
            "background:#1c1c20; border:1px solid #3c3c44; color:#808088;"
        )
        bottom.addWidget(self.resultPreview)

        info = QVBoxLayout()
        info.setSpacing(4)
        self.infoSource = QLabel("原图：—")
        self.infoCrop = QLabel("裁切：—")
        self.infoOut = QLabel("输出：—")
        self.infoHint = QLabel(
            "提示：拖动框内移动，拖八个手柄缩放；\n"
            "方向键微调，Shift+方向键 ×10，Ctrl+方向键缩放"
        )
        self.infoHint.setStyleSheet("color:#909098;")
        for lab in (self.infoSource, self.infoCrop, self.infoOut):
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addWidget(self.infoSource)
        info.addWidget(self.infoCrop)
        info.addWidget(self.infoOut)
        info.addStretch(1)
        info.addWidget(self.infoHint)
        bottom.addLayout(info, 1)
        left.addLayout(bottom, 0)

        root.addLayout(left, 1)
        root.addWidget(self._buildSettingsPanel(), 0)

        self.statusBar().showMessage("就绪")

    def _buildSettingsPanel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(360)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(10)

        # 图源
        gb_src = QGroupBox("图源")
        f_src = QFormLayout(gb_src)
        self.urlEdit = QLineEdit()
        self.urlEdit.setToolTip("NSMC 的 FY4B 全圆盘真彩云图地址")
        f_src.addRow("地址", self.urlEdit)
        v.addWidget(gb_src)

        # 裁切
        gb_crop = QGroupBox("裁切")
        f_crop = QFormLayout(gb_crop)

        row_xy = QHBoxLayout()
        self.xSpin = QSpinBox()
        self.ySpin = QSpinBox()
        for spin, tip in ((self.xSpin, "左上角 X（源图像素）"), (self.ySpin, "左上角 Y（源图像素）")):
            spin.setRange(0, 100000)
            spin.setToolTip(tip)
        row_xy.addWidget(QLabel("X"))
        row_xy.addWidget(self.xSpin)
        row_xy.addWidget(QLabel("Y"))
        row_xy.addWidget(self.ySpin)
        f_crop.addRow("位置", row_xy)

        row_wh = QHBoxLayout()
        self.wSpin = QSpinBox()
        self.hSpin = QSpinBox()
        for spin, tip in ((self.wSpin, "裁切宽度（源图像素）"), (self.hSpin, "裁切高度（源图像素）")):
            spin.setRange(32, 100000)
            spin.setToolTip(tip)
        row_wh.addWidget(QLabel("宽"))
        row_wh.addWidget(self.wSpin)
        row_wh.addWidget(QLabel("高"))
        row_wh.addWidget(self.hSpin)
        f_crop.addRow("大小", row_wh)

        self.aspectCheck = QCheckBox("锁定宽高比")
        f_crop.addRow("", self.aspectCheck)

        self.aspectCombo = QComboBox()
        for name, _ in ASPECTS:
            self.aspectCombo.addItem(name)
        self.aspectCombo.addItem("屏幕比例")
        f_crop.addRow("比例", self.aspectCombo)

        self.stepSpin = QSpinBox()
        self.stepSpin.setRange(1, 1000)
        self.stepSpin.setToolTip("方向键每次移动的源图像素数")
        f_crop.addRow("微调步长", self.stepSpin)

        btn_fit = QPushButton("裁切框居中")
        btn_fit.clicked.connect(self._centerCrop)
        f_crop.addRow("", btn_fit)
        v.addWidget(gb_crop)

        # 输出
        gb_out = QGroupBox("输出")
        f_out = QFormLayout(gb_out)
        self.qualitySpin = QSpinBox()
        self.qualitySpin.setRange(50, 100)
        self.qualitySpin.setToolTip("JPEG 质量，越高越清晰、文件越大")
        f_out.addRow("JPEG 质量", self.qualitySpin)
        self.keepSpin = QSpinBox()
        self.keepSpin.setRange(1, 10)
        self.keepSpin.setToolTip("缓存目录里保留最近几张壁纸")
        f_out.addRow("保留张数", self.keepSpin)
        v.addWidget(gb_out)

        # 定时更新与常驻服务
        gb_up = QGroupBox("定时更新")
        f_up = QFormLayout(gb_up)

        self.minutesEdit = QLineEdit()
        self.minutesEdit.setPlaceholderText("例如 10,25,40,55")
        self.minutesEdit.setToolTip("每小时的哪几分钟更新，逗号分隔")
        f_up.addRow("分钟点", self.minutesEdit)

        self.scheduleCheck = QCheckBox("允许定时更新")
        self.scheduleCheck.setToolTip("关掉后只有你手动点按钮才会更新壁纸")
        f_up.addRow("", self.scheduleCheck)

        self.verifyCheck = QCheckBox("更新后自检并写日志")
        self.verifyCheck.setToolTip("回读桌面环境实际在用的壁纸，和刚写入的比对")
        f_up.addRow("", self.verifyCheck)

        self.roleLabel = QLabel("—")
        self.roleLabel.setWordWrap(True)
        self.roleLabel.setStyleSheet("color:#909098;")
        f_up.addRow("当前", self.roleLabel)

        self.btnTakeOver = QPushButton("接管外部进程")
        self.btnTakeOver.setToolTip(
            "发现别的 FY4B 进程（命令行守护进程之类）在跑时，停掉它并改由本程序负责"
        )
        f_up.addRow("", self.btnTakeOver)
        v.addWidget(gb_up)

        # 轮换保护
        gb_wp = QGroupBox("轮换保护")
        f_wp = QFormLayout(gb_wp)

        self.watchCheck = QCheckBox("壁纸被手动更换时暂停轮换")
        self.watchCheck.setToolTip(
            "定期检查桌面壁纸还是不是本程序设的那张，不是就发桌面通知并停止自动更新"
        )
        f_wp.addRow("", self.watchCheck)

        self.watchSpin = QSpinBox()
        self.watchSpin.setRange(10, 3600)
        self.watchSpin.setSuffix(" 秒")
        self.watchSpin.setToolTip("多久检查一次")
        f_wp.addRow("检测间隔", self.watchSpin)

        self.pauseLabel = QLabel("—")
        self.pauseLabel.setWordWrap(True)
        self.pauseLabel.setStyleSheet("color:#909098;")
        f_wp.addRow("状态", self.pauseLabel)

        pause_row = QHBoxLayout()
        self.btnPause = QPushButton("暂停轮换")
        self.btnResume = QPushButton("恢复轮换")
        self.btnPause.setToolTip("不再自动更换壁纸，直到你手动恢复")
        self.btnResume.setToolTip("重新开始自动轮换，并立刻更新一次")
        pause_row.addWidget(self.btnPause)
        pause_row.addWidget(self.btnResume)
        f_wp.addRow("", pause_row)
        v.addWidget(gb_wp)

        # 运行方式
        gb_run = QGroupBox("运行方式")
        f_run = QFormLayout(gb_run)

        self.modeLabel = QLabel("—")
        self.modeLabel.setWordWrap(True)
        self.modeLabel.setStyleSheet("color:#909098;")
        f_run.addRow("当前模式", self.modeLabel)

        self.trayCheck = QCheckBox("显示托盘图标")
        self.trayCheck.setToolTip(
            "关掉后轻量模式完全没有界面；要回到完整模式得再启动一次本程序"
        )
        f_run.addRow("", self.trayCheck)

        self.autostartCheck = QCheckBox("开机自启动（轻量模式）")
        self.autostartCheck.setToolTip("登录时自动在后台跑，不弹窗口")
        f_run.addRow("", self.autostartCheck)

        self.btnLight = QPushButton("切到轻量模式")
        self.btnLight.setToolTip("把窗口收起来，继续在后台更新；点托盘图标可以回来")
        f_run.addRow("", self.btnLight)

        hint_run = QLabel(
            "关掉窗口 = 切到轻量模式（后台继续跑）。要真正退出，请用托盘菜单里的「退出」。"
        )
        hint_run.setWordWrap(True)
        hint_run.setStyleSheet("color:#7a7a82; font-size:11px;")
        f_run.addRow("", hint_run)
        v.addWidget(gb_run)

        # 后端
        gb_be = QGroupBox("壁纸后端")
        f_be = QFormLayout(gb_be)
        self.backendCombo = QComboBox()
        self.backendCombo.addItems(["auto", "KDE", "niri"])
        f_be.addRow("方式", self.backendCombo)
        self.backendLabel = QLabel("—")
        self.backendLabel.setStyleSheet("color:#909098;")
        f_be.addRow("当前识别", self.backendLabel)
        v.addWidget(gb_be)

        self.configLabel = QLabel(f"配置文件：{FY4B.configPath}")
        self.configLabel.setWordWrap(True)
        self.configLabel.setStyleSheet("color:#7a7a82; font-size:11px;")
        v.addWidget(self.configLabel)
        v.addStretch(1)

        return panel

    # ---------------- 配置 ↔ 界面 ----------------

    def _loadConfigToUi(self) -> None:
        cfg = self.cfg
        self._loading = True
        try:
            self.urlEdit.setText(str(cfg["url"]))
            self.urlEdit.setCursorPosition(0)  # 否则光标停在末尾，URL 开头会被卷走看不见
            self.xSpin.setValue(int(cfg["crop_x"]))
            self.ySpin.setValue(int(cfg["crop_y"]))
            self.wSpin.setValue(int(cfg["crop_w"]))
            self.hSpin.setValue(int(cfg["crop_h"]))
            self.aspectCheck.setChecked(bool(cfg["lock_aspect"]))
            self.stepSpin.setValue(int(cfg["nudge_step"]))
            self.qualitySpin.setValue(int(cfg["jpeg_quality"]))
            self.keepSpin.setValue(int(cfg["keep"]))
            self.scheduleCheck.setChecked(bool(cfg["schedule_enabled"]))
            self.minutesEdit.setText(str(cfg["schedule_minutes"]))
            self.verifyCheck.setChecked(bool(cfg["verify"]))
            self.watchCheck.setChecked(bool(cfg.get("watch_wallpaper", True)))
            self.watchSpin.setValue(int(cfg.get("watch_interval", 60)))
            self.trayCheck.setChecked(bool(cfg.get("enable_tray", True)))
            self.autostartCheck.setChecked(FY4B.isAutostartEnabled())
            self.backendCombo.setCurrentText(str(cfg["wallpaper_backend"]))
        finally:
            self._loading = False

        self.cropView.setStep(self.stepSpin.value())
        self.cropView.setLockAspect(self.aspectCheck.isChecked())
        self.cropView.setAspect(self._currentAspect())
        self.cropView.setBox(
            QRect(self.xSpin.value(), self.ySpin.value(), self.wSpin.value(), self.hSpin.value())
        )
        self._updateInfo()
        self._syncPauseUi()
        self._syncModeUi()

    def _uiToConfig(self) -> dict:
        # 从磁盘重新读一次：暂停状态、last_applied 这些可能被后台检测线程改过
        cfg = FY4B.loadConfig()
        cfg.update(
            {
                "url": self.urlEdit.text().strip(),
                "crop_x": self.xSpin.value(),
                "crop_y": self.ySpin.value(),
                "crop_w": self.wSpin.value(),
                "crop_h": self.hSpin.value(),
                "lock_aspect": self.aspectCheck.isChecked(),
                "nudge_step": self.stepSpin.value(),
                "jpeg_quality": self.qualitySpin.value(),
                "keep": self.keepSpin.value(),
                "schedule_enabled": self.scheduleCheck.isChecked(),
                "schedule_minutes": self.minutesEdit.text().strip() or "10,25,40,55",
                "verify": self.verifyCheck.isChecked(),
                "watch_wallpaper": self.watchCheck.isChecked(),
                "watch_interval": self.watchSpin.value(),
                "enable_tray": self.trayCheck.isChecked(),
                "mode": self.mode,
                "wallpaper_backend": self.backendCombo.currentText(),
            }
        )
        return cfg

    def _currentAspect(self) -> float:
        name = self.aspectCombo.currentText()
        if name == "屏幕比例":
            screen = QApplication.primaryScreen()
            if screen is not None:
                size = screen.size()
                if size.height() > 0:
                    return size.width() / size.height()
        for label, value in ASPECTS:
            if label == name:
                return value
        w, h = self.wSpin.value(), self.hSpin.value()
        return (w / h) if h else (16 / 9)

    # ---------------- 联动 ----------------

    def _bindSignals(self) -> None:
        self.xSpin.valueChanged.connect(lambda v: self._onSpin("x", v))
        self.ySpin.valueChanged.connect(lambda v: self._onSpin("y", v))
        self.wSpin.valueChanged.connect(lambda v: self._onSpin("w", v))
        self.hSpin.valueChanged.connect(lambda v: self._onSpin("h", v))
        self.stepSpin.valueChanged.connect(self.cropView.setStep)
        self.aspectCheck.toggled.connect(self._onAspectToggled)
        self.aspectCombo.currentIndexChanged.connect(self._onAspectChanged)
        self.cropView.boxChanged.connect(self._onBoxChanged)

        self.btnDownload.clicked.connect(lambda: self._runAction("download"))
        self.btnCrop.clicked.connect(lambda: self._runAction("crop"))
        self.btnUpdate.clicked.connect(lambda: self._runAction("update"))
        self.btnSave.clicked.connect(self._saveConfig)
        self.btnReset.clicked.connect(self._resetConfig)
        self.btnTakeOver.clicked.connect(self._onTakeOverClicked)
        self.btnPause.clicked.connect(lambda: self._setPaused(True))
        self.btnResume.clicked.connect(lambda: self._setPaused(False))
        self.btnLight.clicked.connect(lambda: self._applyMode("light"))
        self.trayCheck.toggled.connect(self._onTrayToggled)
        self.autostartCheck.toggled.connect(self._onAutostartToggled)
        self.watchSpin.valueChanged.connect(self._restartWatcher)
        self.bridge.started.connect(self._onScheduledStart)
        self.bridge.finished.connect(self._onScheduledDone)
        self.bridge.failed.connect(self._onScheduledFail)

    def _onSpin(self, which: str, value: int) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            if self.aspectCheck.isChecked() and which in ("w", "h"):
                aspect = self._currentAspect()
                if which == "w":
                    self.hSpin.setValue(max(1, int(round(value / aspect))))
                else:
                    self.wSpin.setValue(max(1, int(round(value * aspect))))
            self.cropView.setBox(
                QRect(
                    self.xSpin.value(),
                    self.ySpin.value(),
                    self.wSpin.value(),
                    self.hSpin.value(),
                )
            )
        finally:
            self._loading = False
        self._updateInfo()

    def _onBoxChanged(self, x: int, y: int, w: int, h: int) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            self.xSpin.setValue(x)
            self.ySpin.setValue(y)
            self.wSpin.setValue(w)
            self.hSpin.setValue(h)
        finally:
            self._loading = False
        self._updateInfo()

    def _onAspectToggled(self, checked: bool) -> None:
        self.cropView.setLockAspect(checked)
        if checked and not self._loading:
            self._loading = True
            try:
                self.cropView.setAspect(self._currentAspect())
                self.cropView.setBox(self.cropView.box())
                box = self.cropView.box()
                self.xSpin.setValue(box.x())
                self.ySpin.setValue(box.y())
                self.wSpin.setValue(box.width())
                self.hSpin.setValue(box.height())
            finally:
                self._loading = False
            self._updateInfo()

    def _onAspectChanged(self, _index: int) -> None:
        self.cropView.setAspect(self._currentAspect())
        if self.aspectCheck.isChecked():
            self._onAspectToggled(True)

    def _centerCrop(self) -> None:
        size = self.cropView.sourceSize()
        if size.isEmpty():
            self.statusBar().showMessage("还没有原图，无法居中")
            return
        w = min(self.wSpin.value(), size.width())
        h = min(self.hSpin.value(), size.height())
        self.cropView.setBox(
            QRect((size.width() - w) // 2, (size.height() - h) // 2, w, h)
        )

    def _updateInfo(self) -> None:
        size = self.cropView.sourceSize()
        box = self.cropView.box()
        if not size.isEmpty():
            self.infoSource.setText(f"原图：{size.width()}×{size.height()}")
        self.infoCrop.setText(
            f"裁切：{box.width()}×{box.height()} @ ({box.x()}, {box.y()})"
        )
        ratio = (box.width() / box.height()) if box.height() else 0
        self.infoOut.setText(
            f"比例：{ratio:.3f}　输出：{box.width()}×{box.height()} JPEG q{self.qualitySpin.value()}"
        )
        pixmap = self.cropView.cropPixmap()
        if pixmap is not None:
            self.resultPreview.setPixmap(
                pixmap.scaled(
                    self.resultPreview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
            )
        else:
            self.resultPreview.setPixmap(QPixmap())
            self.resultPreview.setText("裁切结果")

    # ---------------- 预览 ----------------

    def _loadPreview(self) -> None:
        raw = os.path.join(FY4B.downloadPath, "raw.jpg")
        if not os.path.isfile(raw):
            self.cropView.clearImage()
            self.infoSource.setText("原图：—（还没下载）")
            self._refreshStatus()
            return
        try:
            size = self.cropView.loadImage(raw)
        except Exception as e:
            self.cropView.clearImage()
            self.statusBar().showMessage(f"原图打不开：{type(e).__name__}: {e}")
            return
        # 裁切框的范围跟着原图尺寸走
        self.xSpin.setRange(0, max(0, size.width() - 1))
        self.ySpin.setRange(0, max(0, size.height() - 1))
        self.wSpin.setRange(32, max(32, size.width()))
        self.hSpin.setRange(32, max(32, size.height()))

        self._loading = True
        try:
            # 原图尺寸已知了，这时才能把配置里的裁切框真正套上去
            self.cropView.setBox(
                QRect(
                    self.xSpin.value(),
                    self.ySpin.value(),
                    self.wSpin.value(),
                    self.hSpin.value(),
                )
            )
            box = self.cropView.box()
            self.xSpin.setValue(box.x())
            self.ySpin.setValue(box.y())
            self.wSpin.setValue(box.width())
            self.hSpin.setValue(box.height())
        finally:
            self._loading = False
        self._updateInfo()
        self._refreshStatus()

    def _refreshStatus(self) -> None:
        backend = FY4B.detectBackend(self.cfg)
        self.backendLabel.setText(backend)
        role = self._schedulerRole()
        self.roleLabel.setText(role)
        raw = os.path.join(FY4B.downloadPath, "raw.jpg")
        parts = []
        if os.path.isfile(raw):
            parts.append(f"原图 {os.path.getsize(raw) / 1024 / 1024:.1f} MiB")
        parts.append(f"后端 {backend}")
        parts.append("完整模式" if self.mode == "full" else "轻量模式")
        parts.append(f"自动更新：{role}")
        self.statusBar().showMessage("　|　".join(parts))

    def _schedulerRole(self) -> str:
        """谁在负责自动更新"""
        paused = bool(FY4B.loadConfig().get("rotation_paused"))
        if self.scheduler is not None:
            minutes = self.minutesEdit.text().strip() or "?"
            base = f"本程序（{minutes}）"
            return f"{base} · 已暂停" if paused else base
        pid = FY4B.daemonPid()
        if pid:
            return f"外部进程 PID {pid}"
        return "无人负责（不会自动更新）"

    # ---------------- 动作 ----------------

    BANNER_STYLES = {
        "info": "background:#2a2a32; color:#c8c8d2; border:1px solid #3c3c46;",
        "busy": "background:#2b3342; color:#bcd4f0; border:1px solid #3d5a80;",
        "ok": "background:#20351f; color:#b8e6b0; border:1px solid #3d6b39;",
        "fail": "background:#3a1f1f; color:#f0b8b8; border:1px solid #7a3a3a;",
    }

    def _setBanner(self, text: str, kind: str = "info") -> None:
        style = self.BANNER_STYLES.get(kind, self.BANNER_STYLES["info"])
        self.resultBanner.setStyleSheet(f"padding:6px 10px; border-radius:4px; {style}")
        self.resultBanner.setText(text)

    def _setBusy(self, busy: bool) -> None:
        for btn in (
            self.btnDownload,
            self.btnCrop,
            self.btnUpdate,
            self.btnSave,
            self.btnReset,
            self.btnTakeOver,
        ):
            btn.setEnabled(not busy)
        self.setCursor(Qt.BusyCursor if busy else Qt.ArrowCursor)

    def _persist(self, values: dict) -> None:
        """只改几个键并写回配置（其余键以磁盘上的最新值为准）"""
        self.cfg = self._uiToConfig()
        self.cfg.update(values)
        try:
            FY4B.saveConfig(self.cfg)
        except OSError as e:
            self._setBanner(f"保存失败：{e}", "fail")

    def _saveConfig(self) -> None:
        previous = FY4B.loadConfig()
        self.cfg = self._uiToConfig()
        crop_changed = any(
            previous.get(key) != self.cfg.get(key)
            for key in ("crop_x", "crop_y", "crop_w", "crop_h", "jpeg_quality")
        )
        try:
            FY4B.saveConfig(self.cfg)
        except OSError as e:
            self._setBanner(f"保存失败：{e}", "fail")
            QMessageBox.warning(self, "保存失败", str(e))
            return

        note = ""
        if self.scheduler is not None:
            # 界面正在定时，分钟点改了要立刻生效，不用重开
            minutes = self.minutesEdit.text().strip() or "10,25,40,55"
            try:
                self.scheduler.reschedule_job(
                    "update_wallpaper", trigger="cron", minute=minutes
                )
                note = f"，定时更新改为 {minutes}"
            except Exception as e:
                self._setBanner(
                    f"设置已保存，但重新排程失败：{type(e).__name__}: {e}", "fail"
                )
                self._refreshStatus()
                return

        self._setBanner(f"✓ 设置已保存 → {FY4B.configPath}{note}", "ok")
        self._refreshStatus()
        # 放在 _refreshStatus 之后，否则这条提示会被通用状态冲掉
        if crop_changed:
            self.statusBar().showMessage(
                "裁切/质量已改：点「裁切并应用」立刻生效，否则等下一个更新点"
            )

    def _resetConfig(self) -> None:
        self.cfg = dict(FY4B.DEFAULT_CONFIG)
        self._loadConfigToUi()
        self._loadPreview()
        self._setBanner("已恢复默认值（还没写回文件，点「保存设置」才生效）")
        self.statusBar().showMessage("已恢复默认值（还没写回文件，点「保存设置」才生效）")

    def _runAction(self, action: str) -> None:
        if self.worker is not None and self.worker.isRunning():
            self._setBanner("上一个操作还没结束，请稍等", "busy")
            return

        self.cfg = self._uiToConfig()
        if action in ("crop", "update"):
            # 手动应用时顺手把设置写回文件，免得常驻进程还在用旧配置
            try:
                FY4B.saveConfig(self.cfg)
            except OSError as e:
                self._setBanner(f"保存失败：{e}", "fail")
                QMessageBox.warning(self, "保存失败", str(e))
                return

        busy_text = {
            "download": "正在下载原图…",
            "crop": "正在裁切并应用…",
            "update": "正在完整更新…",
        }[action]
        self._updateRunning = True
        self._setBusy(True)
        self._setBanner(busy_text, "busy")
        self.statusBar().showMessage(busy_text)

        self.worker = Worker(action, self.cfg, self)
        self.worker.progressed.connect(lambda msg: self._setBanner(msg, "busy"))
        self.worker.succeeded.connect(self._onSucceeded)
        self.worker.failed.connect(self._onFailed)
        self.worker.finished.connect(lambda: self._setBusy(False))
        self.worker.start()

    def _onSucceeded(self, message: str) -> None:
        self._updateRunning = False
        self.cfg = FY4B.loadConfig()
        # 结果看横幅，状态栏留给「当前状态」，所以这里不再重复写状态栏
        self._setBanner(f"✓ {message}", "ok")
        if self.worker is not None and self.worker.action in ("download", "update"):
            self._loadPreview()
        self._updateInfo()
        self._syncPauseUi()
        self._refreshStatus()

    def _onFailed(self, message: str) -> None:
        self._updateRunning = False
        self._setBanner(f"✗ {message}", "fail")
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, "操作未完成", message)

    # ---------------- 调度器归属 ----------------

    def _takeOverScheduling(self) -> None:
        """本进程独占调度：先拿文件锁，再按配置起定时任务"""
        pid = FY4B.daemonPid()
        if pid:
            self._setBanner(
                f"检测到外部 FY4B 进程（PID {pid}）在运行，本程序不重复更新壁纸。"
                f"要改由本程序负责，点「接管外部进程」。",
                "info",
            )
            self._refreshStatus()
            return

        FY4B.checkDir(FY4B.downloadPath)  # 全新机器上缓存目录可能还不存在
        try:
            FY4B.lock.acquire(timeout=0)
            self._lockHeld = True
        except Timeout:
            holder = FY4B.lockHolderPid()
            if holder and not FY4B.daemonPid():
                # 占着锁的不是命令行守护进程 → 那就是另一个本程序实例
                self._foreignInstance = True
                self._setBanner(
                    f"已经有一个 FY4B 在运行（PID {holder}），本窗口只用来改配置。",
                    "info",
                )
            else:
                self._setBanner("文件锁被别的实例占着，本程序这次不会自动更新壁纸。", "fail")
            self._refreshStatus()
            return
        except Exception as e:
            self._setBanner(f"获取文件锁失败：{type(e).__name__}: {e}", "fail")
            self._refreshStatus()
            return

        self._startScheduler()
        self._maybeStartupUpdate()

    def _maybeStartupUpdate(self) -> None:
        """
        启动时如果壁纸已经过期，就先更新一张，别干等到下一个分钟点

        开机自启动的场景最明显：10:20 登录，如果分钟点是 10,25,40,55，
        原来的实现要等到 10:25 才有动静。阈值看 startup_max_age（分钟）：
        正数=超过这么久就更新，0=每次都更新，负数=启动不更新。
        """
        cfg = FY4B.loadConfig()
        if not cfg.get("schedule_enabled", True) or cfg.get("rotation_paused"):
            return
        try:
            limit = int(cfg.get("startup_max_age", 20))
        except (TypeError, ValueError):
            limit = 20
        if limit < 0:
            return

        last = str(cfg.get("last_applied") or "")
        if limit > 0 and last and os.path.isfile(last):
            age_min = (time.time() - os.path.getmtime(last)) / 60
            if age_min < limit:
                FY4B.logger.info(
                    f"启动检查：上次更新在 {age_min:.0f} 分钟前（阈值 {limit} 分钟），"
                    f"不重复下载，等下一个更新点 {self.minutesEdit.text().strip()}"
                )
                self._setBanner(
                    f"上次更新在 {age_min:.0f} 分钟前，还够新；"
                    f"等下一个更新点（{self.minutesEdit.text().strip()}）。",
                    "info",
                )
                return
            reason = f"上次更新在 {age_min:.0f} 分钟前，已超过 {limit} 分钟"
        else:
            reason = "还没有可用的上次更新记录"
        FY4B.logger.info(f"启动检查：{reason}，先更新一张再进入定时")
        self._runAction("update")

    def _startScheduler(self) -> None:
        """起定时任务；配置里关掉定时更新就不起"""
        self._stopScheduler()
        cfg = self._uiToConfig()
        if not cfg.get("schedule_enabled", True):
            self._refreshStatus()
            return
        minutes = str(cfg.get("schedule_minutes") or "10,25,40,55")
        try:
            scheduler = BackgroundScheduler()
            scheduler.add_job(
                self._scheduledUpdate, "cron", minute=minutes, id="update_wallpaper"
            )
            scheduler.start()
        except Exception as e:
            self._setBanner(f"定时任务启动失败：{type(e).__name__}: {e}", "fail")
            self._refreshStatus()
            return
        self.scheduler = scheduler
        self._refreshStatus()

    def _stopScheduler(self) -> None:
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass
            self.scheduler = None

    def _releaseLock(self) -> None:
        if self._lockHeld:
            try:
                FY4B.lock.release()
            except Exception:
                pass
            self._lockHeld = False

    def _onTakeOverClicked(self) -> None:
        pid = FY4B.daemonPid()
        if not pid:
            if not self._lockHeld:
                self._takeOverScheduling()
            else:
                self._startScheduler()
            self._setBanner("没有发现外部进程，已由本程序负责自动更新。", "info")
            self._refreshStatus()
            return

        reply = QMessageBox.question(
            self,
            "接管外部进程",
            f"将停止外部 FY4B 进程（PID {pid}），并改由本程序负责自动更新。\n继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return
        if not FY4B.stopDaemon(pid):
            self._setBanner(f"没能停掉外部进程（PID {pid}）", "fail")
            return

        # 对方真正退出后锁才会放开，等一会儿
        for _ in range(50):
            try:
                FY4B.lock.acquire(timeout=0)
                self._lockHeld = True
                break
            except Timeout:
                QApplication.processEvents()
                time.sleep(0.1)
        else:
            self._setBanner("外部进程已退出，但文件锁还没放开，稍后重试。", "fail")
            self._refreshStatus()
            return

        self._startScheduler()
        self._setBanner("✓ 已接管，之后由本程序负责自动更新。", "ok")
        self._refreshStatus()

    # ---------------- 定时任务 ----------------

    def _scheduledUpdate(self) -> None:
        """跑在 APScheduler 的线程里，不能直接碰界面控件，只能发信号"""
        if self._updateRunning:
            FY4B.logger.info("正在手动更新，跳过本次定时任务")
            return
        self.bridge.started.emit()
        try:
            path = FY4B.update()
            self.bridge.finished.emit(path or "")
        except Exception as e:
            self.bridge.failed.emit(f"{type(e).__name__}: {e}")

    def _onScheduledStart(self) -> None:
        self._updateRunning = True
        self._setBanner("定时更新中…", "busy")

    def _onScheduledDone(self, path: str) -> None:
        self._updateRunning = False
        if not path:
            self._setBanner("轮换处于暂停状态，本次定时更新已跳过。", "info")
            self._refreshStatus()
            return
        self._setBanner(f"✓ 定时更新完成：{os.path.basename(path)}", "ok")
        self._loadPreview()
        self._updateInfo()
        self._refreshStatus()

    def _onScheduledFail(self, message: str) -> None:
        self._updateRunning = False
        self._setBanner(f"✗ 定时更新失败：{message}", "fail")
        self._refreshStatus()

    # ---------------- 壁纸被换掉的监视 ----------------

    def _startWatcher(self) -> None:
        self._restartWatcher()

    def _restartWatcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
        try:
            interval = max(10, int(FY4B.loadConfig().get("watch_interval", 60) or 60))
        except (TypeError, ValueError):
            interval = 60
        self._watcher = QTimer(self)
        self._watcher.setInterval(interval * 1000)
        self._watcher.timeout.connect(self._watchTick)
        self._watcher.start()

    def _watchTick(self) -> None:
        if self._updateRunning:
            return  # 自己正在换壁纸，别误判
        try:
            if not FY4B.checkWallpaperReplaced():
                return
        except Exception as e:
            FY4B.logger.warning(f"壁纸检查失败：{type(e).__name__}: {e}")
            return
        self.cfg = FY4B.loadConfig()
        self._syncPauseUi()
        self._updateTrayPauseActions()
        self._setBanner(
            "检测到壁纸被手动更换，已暂停自动轮换并发了一条桌面通知。"
            "点「恢复轮换」可以继续。",
            "fail",
        )
        self._refreshStatus()

    def _firstWallpaperCheck(self) -> None:
        """启动后稍等一下再查一次，上次退出期间被换掉也能发现"""
        if self.mode == "light" and not self.trayCheck.isChecked():
            return
        self._watchTick()

    def _setPaused(self, paused: bool) -> None:
        self.cfg = FY4B.setPaused(paused)
        self._syncPauseUi()
        self._updateTrayPauseActions()
        self._refreshStatus()
        if paused:
            self._setBanner("已暂停自动轮换，壁纸不会再被自动更换。", "info")
        else:
            self._setBanner("✓ 已恢复自动轮换，正在更新一次…", "ok")
            self._runAction("update")

    def _syncPauseUi(self) -> None:
        cfg = FY4B.loadConfig()
        paused = bool(cfg.get("rotation_paused"))
        self._loading = True
        try:
            self.btnPause.setEnabled(not paused)
            self.btnResume.setEnabled(paused)
        finally:
            self._loading = False
        if paused:
            when = cfg.get("paused_at") or "未知时间"
            self.pauseLabel.setText(f"已暂停（{when}）")
        else:
            self.pauseLabel.setText("正常轮换中")

    # ---------------- 运行模式 ----------------

    def _applyMode(self, mode: str, initial: bool = False) -> None:
        """
        模式只决定「窗口显不显示」，调度器一直在本进程里跑，
        所以切换不重启进程、不碰文件锁，也就不会出现多实例
        """
        self.mode = "light" if mode == "light" else "full"
        if self.mode == "full":
            self.showNormal()
            self.raise_()
            self.activateWindow()
        else:
            self.hide()
        self._syncModeUi()
        if initial:
            return
        self._refreshStatus()
        self._persist({"mode": self.mode})
        if self.mode == "light":
            if self.trayCheck.isChecked():
                tip = "已切到轻量模式，在托盘里继续跑。"
            else:
                tip = "已切到轻量模式（没有托盘）。想回到窗口，再启动一次本程序即可。"
            self._setBanner(tip, "info")
        else:
            self._setBanner("已切到完整模式。", "info")

    def _syncModeUi(self) -> None:
        self.modeLabel.setText(
            "完整模式（窗口已打开）" if self.mode == "full" else "轻量模式（后台运行）"
        )
        if self.trayModeFull is not None:
            self.trayModeFull.setChecked(self.mode == "full")
        if self.trayModeLight is not None:
            self.trayModeLight.setChecked(self.mode == "light")

    # ---------------- 托盘 ----------------

    def _setupTray(self) -> None:
        enabled = self.trayCheck.isChecked() and QSystemTrayIcon.isSystemTrayAvailable()
        if not enabled:
            if self.tray is not None:
                self.tray.hide()
                self.tray = None
            self.trayModeFull = None
            self.trayModeLight = None
            self.trayPauseAction = None
            self.trayResumeAction = None
            self.trayToggleAction = None
            return
        if self.tray is not None:
            self._updateTrayPauseActions()
            return

        icon = QIcon.fromTheme("preferences-desktop-wallpaper")
        if icon.isNull():
            icon = QIcon.fromTheme("video-display")
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.SP_ComputerIcon)

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("FY4B 壁纸")

        menu = QMenu()
        self.trayModeFull = menu.addAction("完整模式（显示窗口）")
        self.trayModeFull.setCheckable(True)
        self.trayModeFull.triggered.connect(lambda: self._applyMode("full"))
        self.trayModeLight = menu.addAction("轻量模式（后台运行）")
        self.trayModeLight.setCheckable(True)
        self.trayModeLight.triggered.connect(lambda: self._applyMode("light"))
        menu.addSeparator()
        act_update = menu.addAction("立即更新")
        act_update.triggered.connect(lambda: self._runAction("update"))
        self.trayPauseAction = menu.addAction("暂停轮换")
        self.trayPauseAction.triggered.connect(lambda: self._setPaused(True))
        self.trayResumeAction = menu.addAction("恢复轮换")
        self.trayResumeAction.triggered.connect(lambda: self._setPaused(False))
        self.trayToggleAction = menu.addAction("开机自启动")
        self.trayToggleAction.setCheckable(True)
        self.trayToggleAction.setChecked(FY4B.isAutostartEnabled())
        self.trayToggleAction.toggled.connect(self._onAutostartToggled)
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self._reallyQuit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._onTrayActivated)
        self.tray.show()
        self._updateTrayPauseActions()
        self._syncModeUi()

    def _updateTrayPauseActions(self) -> None:
        paused = bool(FY4B.loadConfig().get("rotation_paused"))
        if self.trayPauseAction is not None:
            self.trayPauseAction.setEnabled(not paused)
        if self.trayResumeAction is not None:
            self.trayResumeAction.setEnabled(paused)
        if self.tray is not None:
            self.tray.setToolTip(
                "FY4B 壁纸（已暂停轮换）" if paused else "FY4B 壁纸（自动更新中）"
            )

    def _onTrayToggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._setupTray()
        self._persist({"enable_tray": checked})
        if checked and self.tray is None:
            self._setBanner("这个桌面没有系统托盘，图标开不起来。", "fail")
        elif not checked and self.mode == "light":
            self._setBanner(
                "托盘已关闭。你正在轻量模式，想回到窗口就再启动一次本程序。", "info"
            )

    def _onTrayActivated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._applyMode("full" if self.mode == "light" else "light")

    def _onAutostartToggled(self, checked: bool) -> None:
        if self._loading:
            return
        ok = FY4B.setAutostart(checked)
        actual = FY4B.isAutostartEnabled()
        self._loading = True
        try:
            self.autostartCheck.setChecked(actual)
            if self.trayToggleAction is not None:
                self.trayToggleAction.setChecked(actual)
        finally:
            self._loading = False
        if not ok:
            self._setBanner("自启动设置失败，看看日志。", "fail")
        elif checked:
            self._setBanner("✓ 已开启开机自启动（登录后用轻量模式在后台跑）", "ok")
        else:
            self._setBanner("已关闭开机自启动。", "info")

    def _reallyQuit(self) -> None:
        self._stopScheduler()
        self._releaseLock()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        # 关窗口 = 切到轻量模式，后台继续跑；真正退出走托盘菜单的「退出」
        if self.tray is not None:
            event.ignore()
            self._applyMode("light")
            if not self._trayNotified:
                self.tray.showMessage(
                    "FY4B 已转到后台",
                    "自动更新继续运行。要退出请右键托盘图标选「退出」。",
                )
                self._trayNotified = True
            return

        reply = QMessageBox.question(
            self,
            "退出 FY4B？",
            "托盘图标已关闭，关掉窗口就会退出程序，之后不再自动更新壁纸。\n确定退出吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            event.ignore()
            return
        self._stopScheduler()
        self._releaseLock()
        event.accept()

    # ---------------- 单实例：再次启动就把已有窗口叫出来 ----------------

    def startIpcServer(self, clean: bool = True) -> bool:
        if clean:
            QLocalServer.removeServer(SERVER_NAME)  # 清掉上次崩溃留下的 socket 文件
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._onIpcConnection)
        self.ipcListening = bool(self.server.listen(SERVER_NAME))
        return self.ipcListening

    def _onIpcConnection(self) -> None:
        conn = self.server.nextPendingConnection() if self.server else None
        if conn is not None:
            conn.close()
            conn.deleteLater()
        self._applyMode("full")  # 有人又启动了本程序 → 把窗口给他
        self.showNormal()
        self.raise_()
        self.activateWindow()


def _notifyExistingInstance(retries: int = 1) -> bool:
    """通知已经在跑的实例把窗口显示出来；成功返回 True"""
    for attempt in range(max(1, retries)):
        probe = QLocalSocket()
        probe.connectToServer(SERVER_NAME)
        if probe.waitForConnected(500):
            probe.write(b"show\n")
            probe.flush()
            probe.waitForBytesWritten(500)
            probe.disconnectFromServer()
            return True
        if attempt + 1 < retries:
            time.sleep(0.2)
    return False


def main() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("FY4B")
    app.setDesktopFileName("fy4b-wallpaper")

    mode = None
    if "--light" in sys.argv:
        mode = "light"
    elif "--full" in sys.argv:
        mode = "full"

    # 已经有实例在跑 → 把它的窗口叫出来，自己直接退出，绝不起第二个进程
    if _notifyExistingInstance():
        print("FY4B 已经在运行，已通知它显示窗口。")
        return 0

    QLocalServer.removeServer(SERVER_NAME)  # 清掉上次崩溃留下的 socket 文件
    FY4B.initLog()
    window = MainWindow(mode=mode)

    if window._foreignInstance or not window.ipcListening:
        # 锁或单实例名字被别人占了 → 再确认一次是不是另一个本程序实例
        if _notifyExistingInstance(retries=25):
            window._stopScheduler()
            window._releaseLock()
            print("FY4B 已经在运行，已通知它显示窗口。")
            return 0
        print("检测到另一个 FY4B 实例，但联系不上它，退出以避免出现多个实例。")
        window._stopScheduler()
        window._releaseLock()
        return 1

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
