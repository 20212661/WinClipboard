# -*- coding: utf-8 -*-
"""图钉画布存储:图片资产(pins/images,sha256 去重)+ 画板状态(pins/board.json)。

独立于剪贴板缓存(cm.IMAGE_DIR):历史删除 / 上限淘汰 / 启动孤儿清理都不会
影响已钉图片(验收场景 F)。写入走临时文件原子替换,损坏回退 .bak1,
再坏则空板重启(绝不拖垮主程序)。本模块不依赖 Qt。
"""
import hashlib
import json
import os
import traceback
from pathlib import Path

import clipboard_manager as cm

IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def _safe_child(directory, name):
    """把 name 净化为 directory 下的安全子路径;任何穿越企图返回空串。"""
    fn = os.path.basename(str(name))
    if not fn or ".." in fn:
        return ""
    root = os.path.normpath(directory)
    dst = os.path.normpath(root + os.sep + fn)
    if not dst.startswith(root + os.sep):
        return ""
    return dst


class PinBoardStorage:
    """pins/ 目录的读写门面;app_dir=None 时为纯内存模式(预览替身/无持久化)。"""

    def __init__(self, app_dir):
        self.root = os.path.join(app_dir, "pins") if app_dir else None
        self.img_dir = os.path.join(self.root, "images") if self.root else None
        self.board_file = os.path.join(self.root, "board.json") if self.root else None
        if self.img_dir:
            try:
                os.makedirs(self.img_dir, exist_ok=True)
            except Exception:
                cm.log("图钉画布目录创建失败:\n%s" % traceback.format_exc())

    # ---- 资产 ----
    def asset_path(self, name):
        """资产绝对路径;文件名经 basename 净化并做包含校验,拒绝穿越。"""
        if not self.img_dir:
            return ""
        return _safe_child(self.img_dir, name)

    def import_asset(self, src):
        """把外部图片收编为画布资产:sha256 为文件名,同内容只存一份。
        来源限于用户主动钉入的图片文件(扩展名白名单)。返回 (name, w, h);
        失败返回 (None, 0, 0)。"""
        if not self.img_dir:
            return None, 0, 0
        src = os.path.normpath(os.path.abspath(str(src)))
        ext = os.path.splitext(src)[1].lower()
        if ext not in IMG_EXTS or not os.path.isfile(src):
            return None, 0, 0
        try:
            with open(src, "rb") as f:
                data = f.read()
            if not data:
                return None, 0, 0
            name = hashlib.sha256(data).hexdigest() + ext
            dst = self.asset_path(name)
            if not dst:
                return None, 0, 0
            if not os.path.exists(dst):
                tmp = dst + ".tmp"
                Path(tmp).write_bytes(data)
                os.replace(tmp, dst)
        except Exception:
            cm.log("图钉资产导入失败(%s):\n%s" % (src, traceback.format_exc()))
            return None, 0, 0
        w = h = 0
        try:
            with cm.Image.open(dst) as im:      # 只读尺寸(PIL,不依赖 Qt)
                w, h = im.size
        except Exception:
            pass
        return name, w, h

    def sweep(self, keep_names):
        """删除不被任何条目引用的孤儿资产;返回删除数。"""
        if not self.img_dir or not os.path.isdir(self.img_dir):
            return 0
        keep = {os.path.basename(str(n)) for n in keep_names}
        removed = 0
        for fn in os.listdir(self.img_dir):
            if fn in keep or not fn.lower().endswith(IMG_EXTS):
                continue
            try:
                os.remove(os.path.join(self.img_dir, fn))
                removed += 1
            except Exception:
                pass
        return removed

    # ---- 画板状态 ----
    def load_state(self):
        """读 board.json(主文件损坏回退 .bak1);返回 dict 或 None。"""
        if not self.board_file:
            return None
        for fp in (self.board_file, self.board_file + ".bak1"):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except FileNotFoundError:
                continue
            except Exception:
                cm.log("图钉画布状态文件损坏(%s),尝试备份" % fp)
                continue
        return None

    def save_state(self, state):
        if not self.board_file:
            return
        tmp = self.board_file + ".tmp"
        try:
            if os.path.exists(self.board_file):
                os.replace(self.board_file, self.board_file + ".bak1")
            Path(tmp).write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.replace(tmp, self.board_file)
        except Exception:
            cm.log("图钉画布状态写入失败:\n%s" % traceback.format_exc())
            try:
                os.remove(tmp)
            except OSError:
                pass

    def migrate_from_config(self, cfg):
        """旧版把画布存在 config.json["pinboard"](直接引用剪贴板缓存路径):
        一次性把仍存在的图片收编为独立资产。board.json 已存在时跳过。
        返回旧状态 dict(含 geo/open)或 None。"""
        if not self.board_file or os.path.exists(self.board_file):
            return None
        old = cfg.get("pinboard") if isinstance(cfg, dict) else None
        if not isinstance(old, dict):
            return None
        return old
