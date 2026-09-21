# -*- coding: utf-8 -*-
"""PinBoardManager:图钉画布的门面。

负责窗口生命周期(独立于主窗口存在,程序退出才销毁)、状态持久化
(pins/board.json,800ms 防抖)、图片资产生命周期(pins/images,sha256 去重,
与剪贴板缓存完全隔离)、剪贴板直钉(Ctrl+Shift+P / Ctrl+V)与鼠标穿透
(Ctrl+Alt+T)。主程序只与本类交互,不直接碰 window/scene。
"""
import os
import tempfile
import time
import traceback

from PySide6.QtCore import Qt, QObject, QTimer, QPointF
from PySide6.QtGui import QGuiApplication, QImage, QPixmap, QCursor

import clipboard_manager as cm
from perf_probe import PerfSpan

from .loader import PinImageLoader
from .model import PinRecord, MIN_SCALE
from . import annotations as _anno
from .storage import PinBoardStorage, IMG_EXTS
from .window import PinBoardWindow

SAVE_DEBOUNCE_MS = 800
PREVIEW_CACHE_CAP = 200      # preview pixmap 内存缓存上限(小图,张数够用)
# 视口感知加载:可见区四周的预加载边距(场景坐标)——刚拖出/缩放出边缘的
# 图提前开始解码,到达时不等待;滚出该范围的条目退回 metadata(占位框)
VIEWPORT_PRELOAD_MARGIN = 500.0
# 全分辨率缓存按字节预算淘汰(而非张数):张数上限对图片尺寸没有意义——
# 1080p RGBA ≈ 7.9MB、4K ≈ 31.6MB、8K ≈ 126.6MB。384MB ≈ 12 张 1080p /
# 3 张 4K,后续可再接 min(用户配置, 物理内存×8%)
FULL_CACHE_BYTES_LIMIT = 384 * 1024 * 1024


class _FullEntry:
    """全图缓存条目:pixmap + 字节数(解码时一次算好,命中零开销)。"""

    __slots__ = ("pixmap", "bytes")

    def __init__(self, pixmap, nbytes):
        self.pixmap = pixmap
        self.bytes = nbytes


class PinBoardManager(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.persist = getattr(app, "cfg", None) is not None
        self.storage = PinBoardStorage(cm.APP_DIR if self.persist else None)
        self.img_exts = IMG_EXTS

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(lambda: self.save_now())

        # 视口感知加载的节流定时器(重绘驱动,见 view.BoardView.paintEvent)
        self._visible_timer = QTimer(self)
        self._visible_timer.setSingleShot(True)
        self._visible_timer.setInterval(150)
        self._visible_timer.timeout.connect(self._check_visible)

        self._previews = {}          # asset -> QPixmap(降采样)
        self._full_cache = {}        # asset -> _FullEntry(全分辨率,字节预算 LRU)
        self._full_cache_bytes = 0   # 缓存内条目字节累计(淘汰依据)
        self.records = []            # PinRecord 列表(与窗口条目一一对应)
        self._assets_dirty = False   # 资产增删后才需要清扫孤儿(几何变化不用扫)
        self._ctt_hotkey_ok = True   # Ctrl+Alt+T 是否注册成功(失败则禁穿透)
        self._ctt = False

        self.loader = PinImageLoader()
        self.loader.loaded.connect(self._on_loaded)

        self.window = PinBoardWindow(self)
        if getattr(app, "safe_mode", False):
            pass    # 安全模式:画布窗口空壳,不加载钉住的图片(建议 20)
        else:
            self._restore()

    # ---- 启动恢复 ----
    def _restore(self):
        if not self.persist:
            return
        recs = []
        geo = view = None
        open_ = False
        state = self.storage.load_state()
        if state is None:
            old = self.storage.migrate_from_config(self.app.cfg)
            if old is not None:
                geo, open_ = old.get("geo"), bool(old.get("open"))
                missing = 0
                for d in old.get("items") or []:
                    r0 = PinRecord.from_dict(d)
                    if r0 is None:
                        continue
                    src = str(d.get("path") or "")
                    name, w, h = self.storage.import_asset(src)
                    if name is None:
                        missing += 1
                        continue
                    r0.asset, r0.w, r0.h = name, (w or r0.w), (h or r0.h)
                    recs.append(r0)
                if missing:
                    cm.log("图钉画布迁移:%d 张图片文件已丢失,跳过" % missing)
                cm.log("图钉画布迁移:从 config.json 收编 %d 张" % len(recs))
                self.records = recs
                self.window.restore(recs, geo=geo, zoom=None, open_=open_)
                # 迁移即落盘:下次启动走 board.json,不再依赖旧 config 键
                self.save_now(open_=open_)
                return
        else:
            geo, view, open_ = state.get("geo"), state.get("view"), \
                bool(state.get("open"))
            missing = 0
            for d in state.get("items") or []:
                r = PinRecord.from_dict(d)
                if r is None or not os.path.exists(self.storage.asset_path(r.asset)):
                    missing += 1
                    continue
                recs.append(r)
            if missing:
                cm.log("图钉画布恢复:%d 张图片文件缺失,已跳过" % missing)
        self.records = recs
        zoom, center = self._parse_view(view)
        self.window.restore(recs, geo=geo, zoom=zoom, open_=open_,
                            center=center)
        if recs:
            cm.log("图钉画布已恢复:%d 张" % len(recs))
        if self.storage.img_dir:
            self.storage.sweep(r.asset for r in self.records)

    # ---- 钉图入口 ----
    def pin_path(self, path, scene_pos=None, show=True, activate=True):
        """钉一张图片文件:收编资产 → 建条目 → 显示画布。失败返回 None。"""
        if self.storage.img_dir:
            name, w, h = self.storage.import_asset(path)
            if name is None:
                return None
        else:                         # 纯内存模式(预览替身):直接引用源文件
            name = os.path.normpath(os.path.abspath(str(path)))
            if not os.path.isfile(name) or \
                    os.path.splitext(name)[1].lower() not in IMG_EXTS:
                return None
            w = h = 0
            try:
                with cm.Image.open(name) as im:
                    w, h = im.size
            except Exception:
                pass
        rec = PinRecord(asset=name, w=w, h=h)
        if scene_pos is not None:
            rec.x, rec.y = scene_pos.x(), scene_pos.y()
        else:
            k = len(self.records)
            rec.x, rec.y = 14 + (k % 6) * 26, 14 + (k // 6) * 26
        if show:
            self.show_board(activate=activate)
        self.records.append(rec)
        self._assets_dirty = True
        it = self.window.add_record_item(rec, fit_big=True)
        self.submit_preview(rec)
        self.schedule_save()
        cm.log("图钉画布:钉入图片 %s" % os.path.basename(name)[:24])
        return it

    def show_board(self, activate=True):
        w = self.window
        if activate:
            w.show()
            w.raise_()
            w.activateWindow()
        else:
            # 热键直钉场景:显示但不抢焦点(用户可能正在别处输入)
            w.setAttribute(Qt.WA_ShowWithoutActivating, True)
            w.show()
            w.raise_()
            w.setAttribute(Qt.WA_ShowWithoutActivating, False)

    # ---- 剪贴板直钉(Ctrl+Shift+P / Ctrl+V) ----
    def pin_clipboard_image(self, near_cursor=True):
        """把当前剪贴板图片钉到画布(不产生剪贴板历史);无图片时静默。"""
        clip = QGuiApplication.clipboard()
        pm = clip.image()
        if not pm.isNull():
            tmp = os.path.join(tempfile.gettempdir(),
                               "wclb_pin_%d.png" % int(time.time() * 1000))
            try:
                if not pm.save(tmp, "PNG"):
                    return None
                return self._pin_file(tmp, near_cursor=near_cursor)
            finally:
                try:
                    os.remove(tmp)   # 资产已复制,临时文件即删
                except OSError:
                    pass
        # 剪贴板里是图片文件(如从资源管理器复制的) → 钉文件本体
        for u in clip.mimeData().urls() if clip.mimeData() else []:
            if u.isLocalFile() and str(u.toLocalFile()).lower().endswith(
                    IMG_EXTS):
                return self._pin_file(str(u.toLocalFile()), near_cursor=near_cursor)
        return None

    def _pin_file(self, path, near_cursor):
        if near_cursor:
            cur = QCursor.pos()
            w = self.window
            if not w.isVisible():
                self._move_board_to_screen_at(cur)
            self.show_board(activate=False)
            vp = w.view.mapFromGlobal(cur)
            r = w.view.viewport().rect()
            if r.contains(vp):
                sp = w.view.mapToScene(vp)
                mrg = 60
                sr = w.scene.sceneRect()
                sp.setX(max(sr.left() + mrg, min(sr.right() - mrg, sp.x())))
                sp.setY(max(sr.top() + mrg, min(sr.bottom() - mrg, sp.y())))
                return self.pin_path(path, scene_pos=sp, show=False)
        return self.pin_path(path)

    def _move_board_to_screen_at(self, gpos):
        """画布隐藏时,把窗口挪到光标所在屏幕的中心区域(多显示器直钉)。"""
        w = self.window
        scr = QGuiApplication.screenAt(gpos) or QGuiApplication.primaryScreen()
        if scr is None:
            return
        g = scr.availableGeometry()
        w.move(g.left() + (g.width() - w.width()) // 2,
               g.top() + (g.height() - w.height()) // 2)

    def paste_from_clipboard(self, center=False):
        """画布内 Ctrl+V / 菜单粘贴:图片或图片文件,否则忽略。"""
        if center and not self.window.isVisible():
            self.show_board(activate=True)
        it = self.pin_clipboard_image(near_cursor=not center)
        if it is None and self.window.isVisible():
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.info("剪贴板没有图片", "复制一张图片后再粘贴到画布",
                         parent=self.window, position=InfoBarPosition.TOP,
                         duration=2000)
        return it

    # ---- 条目移除 ----
    def on_item_removed(self, rec):
        try:
            self.records.remove(rec)
        except ValueError:
            pass
        self._assets_dirty = True
        self.schedule_save()      # save_now 内顺带清扫孤儿资产

    # ---- pixmap 装填 ----
    def submit_preview(self, rec):
        pm = self._previews.get(rec.asset)
        if pm is not None:
            for it in self.window.items_for_asset(rec.asset):
                it.set_preview(pm)
            return
        self.loader.submit(rec.asset, self.asset_full(rec.asset))

    # ---- 视口感知加载 ----
    def schedule_visible_check(self):
        """视口可能变了:节流 150ms 后扫描(连续拖动期间不会每帧重扫)。"""
        if not self._visible_timer.isActive():
            self._visible_timer.start()

    def check_visible_now(self):
        """立即扫描一次(恢复期等不进重绘的场合调用)。"""
        self._visible_timer.stop()
        self._check_visible()

    def _check_visible(self):
        """可见区 + 预加载边距内的条目确保有 preview(或恢复 1:1 全图);
        滚出范围的条目释放全分辨率 pixmap,只剩 metadata(占位框)。"""
        w = self.window
        items = w.pin_items()
        if not items:
            return
        m = VIEWPORT_PRELOAD_MARGIN
        vr = w.visible_rect().adjusted(-m, -m, m, m)
        for it in items:
            try:
                if it.mapRectToScene(it.boundingRect()).intersects(vr):
                    it.ensure_preview()
                else:
                    it.release_full_viewport()
            except RuntimeError:      # C++ 侧已销毁的极端时序
                continue

    def preview_pixmap(self, name):
        return self._previews.get(name)

    def _on_loaded(self, name, img):
        if img.isNull():
            cm.log("图钉画布图片解码失败:%s" % os.path.basename(str(name))[:40])
            return
        pm = QPixmap.fromImage(img)
        self._previews[name] = pm
        while len(self._previews) > PREVIEW_CACHE_CAP:
            self._previews.pop(next(iter(self._previews)))
        for it in self.window.items_for_asset(name):
            if it.rec.w == 0:
                it.rec.w, it.rec.h = img.width(), img.height()
            it.set_preview(pm)

    def request_full(self, it):
        """1:1 显示需要全分辨率 pixmap;字节预算 LRU 防止大图堆积吃内存。
        (普通 dict 按插入序当 LRU 用:命中即删了重插到末尾。)"""
        name = it.rec.asset
        entry = self._full_cache.pop(name, None)
        if entry is None:
            with PerfSpan("pin_full_image_decode"):
                img = QImage(self.asset_full(name))
                if img.isNull():
                    return
                pm = QPixmap.fromImage(img)
            # bytesPerLine×height 已含行对齐填充,是真实占用;不用
            # pixmap.toImage() 估——那会整块深拷贝,只为量尺寸
            entry = _FullEntry(pm, img.bytesPerLine() * img.height())
        self._full_cache[name] = entry
        self._full_cache_bytes += entry.bytes
        self._evict_full()
        it.set_full(entry.pixmap)

    def _evict_full(self):
        while self._full_cache_bytes > FULL_CACHE_BYTES_LIMIT and self._full_cache:
            victim, entry = next(iter(self._full_cache.items()))
            del self._full_cache[victim]
            self._full_cache_bytes -= entry.bytes
            # 非当前 1:1 显示的条目退回 preview
            for other in self.window.items_for_asset(victim):
                other.use_preview()

    def asset_full(self, name):
        p = self.storage.asset_path(name)
        return p if p else str(name)

    @staticmethod
    def _parse_view(view):
        """view = [zoom, cx, cy](旧格式只有标量 zoom 或缺失)。"""
        if isinstance(view, (list, tuple)) and len(view) == 3:
            try:
                z, cx, cy = (float(view[0]), float(view[1]), float(view[2]))
                return z, QPointF(cx, cy)
            except (TypeError, ValueError):
                pass
        if isinstance(view, (int, float)):
            return float(view), None
        return None, None

    # ---- 编辑回写(标注/修改后原地替换) ----
    def replace_asset_from(self, it, result):
        """编辑器产物(路径 str 或 PIL.Image)替换条目资产;几何/外观不变。"""
        tmp = None
        try:
            if isinstance(result, str):
                src = result
            else:                        # PIL.Image → 落临时 PNG 再收编
                tmp = os.path.join(
                    tempfile.gettempdir(),
                    "wclb_pin_edit_%d.png" % int(time.time() * 1000))
                result.save(tmp, "PNG")
                src = tmp
            if self.storage.img_dir:
                name, w, h = self.storage.import_asset(src)
                if name is None:
                    return False
            else:                        # 纯内存模式:直接引用源文件
                name = os.path.normpath(os.path.abspath(src))
                w = h = 0
                try:
                    with cm.Image.open(name) as im:
                        w, h = im.size
                except Exception:
                    pass
            it.rec.asset = name
            it.rec.w, it.rec.h = w or it.rec.w, h or it.rec.h
            self._install_preview(it, name)
            self.window._sync_scene_rect()
            self._assets_dirty = True
            self.schedule_save()         # 旧资产随落盘清扫
            return True
        except Exception:
            cm.log("图钉画布编辑回写失败:\n%s" % traceback.format_exc())
            return False
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _install_preview(self, it, name):
        """同步生成并装填 preview(单图一次解码,编辑回写场景可接受)。"""
        from .loader import MAX_PREVIEW_EDGE
        img = QImage(self.asset_full(name))
        if img.isNull():
            it.set_preview(QPixmap())    # 装填失败 → 占位框
            return
        big = max(img.width(), img.height())
        if big > MAX_PREVIEW_EDGE:
            k = MAX_PREVIEW_EDGE / float(big)
            img = img.scaled(max(1, int(img.width() * k)),
                             max(1, int(img.height() * k)),
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pm = QPixmap.fromImage(img)
        self._previews[name] = pm
        it.set_preview(pm)

    # ---- 非破坏标注层 ----
    def set_annotations(self, it, annos):
        """写标注层:只改 rec.annos + 画布覆盖显示,资产文件不动。"""
        try:
            clean = _anno.sanitize(annos)
            it.rec.annos = clean
            it.rebuild_annos()
            self.schedule_save()
            return True
        except Exception:
            cm.log("图钉画布标注层写入失败:\n%s" % traceback.format_exc())
            return False

    def composed_image(self, rec):
        """复制/导出用合成图:原图 + 标注层(无标注即原图)。"""
        with cm.Image.open(self.asset_full(rec.asset)) as im:
            base = im.convert("RGB")
        return _anno.compose(base, rec.annos)

    # ---- 持久化 ----
    def schedule_save(self):
        if self.persist:
            self._save_timer.start()

    def save_now(self, open_=None):
        if not self.persist:
            return
        try:
            self.window.sync_records()
            w = self.window
            ctr = w.view.mapToScene(w.view.viewport().rect().center())
            z = w.view.transform().m11() or 1.0
            self.storage.save_state({
                "version": 2,          # v2:items[] 增加 annos 标注层(非破坏模型)
                "geo": [w.x(), w.y(), w.width(), w.height()],
                "open": w.isVisible() if open_ is None else bool(open_),
                "view": [round(z, 4), round(ctr.x(), 2), round(ctr.y(), 2)],
                "items": [r.to_dict() for r in self.records],
            })
            # 只有资产增删过才扫描目录(几何调整的防抖落盘不必每次 listdir)
            if self.storage.img_dir and self._assets_dirty:
                self.storage.sweep(r.asset for r in self.records)
                self._assets_dirty = False
        except Exception:
            cm.log("图钉画布保存失败:\n%s" % traceback.format_exc())

    # ---- 鼠标穿透 ----
    def mark_ctt_hotkey(self, ok):
        """TrayWorker 告知 Ctrl+Alt+T 注册结果;失败则禁用穿透(防自锁)。"""
        self._ctt_hotkey_ok = bool(ok)

    def toggle_click_through(self, on=None):
        target = (not self._ctt) if on is None else bool(on)
        if target and not self._ctt_hotkey_ok:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.error("无法进入穿透", "退出热键(Ctrl+Alt+T)被占用",
                          parent=self.window, position=InfoBarPosition.TOP,
                          duration=2500)
            return
        if not self.window.isVisible():
            self.show_board(activate=False)
        self._ctt = target
        self.window.set_click_through(target)
