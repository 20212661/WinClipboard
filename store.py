# -*- coding: utf-8 -*-
"""SQLite 历史存储(clipboard.db)—— 剪贴板条目的事实存储。

设计:
- 表 clipboard_items:用户可见字段(created_at/content_type/content_subtype/
  content_hash/source_app/text_content/blob_*/is_pinned/…)+ seq 显式顺序列
  (列表语义 = 最近在前;dedup 提升与撤销恢复都取 max(seq)+1)。
- 全文搜索 FTS5(unicode61);CJK 无分词,查询含非 ASCII 时自动退回
  LIKE '%q%'(参数绑定),两类路径都只返回 id 列表,由上层回内存对象。
- 图片二进制不入库:blob_hash 等元数据列指向磁盘上按内容寻址的
  images/img_<hash>.png(与现行完全一致;分片目录布局留待后续阶段)。
- 所有 SQL 一律参数绑定,禁止拼接。
- 写入策略:单写者(GUI 线程)逐操作写透,WAL 模式;
  load_all() 只在启动/测试重载时调用。

兼容:is_sensitive 条目不入本表(私密区 private.json + DPAPI 独立存储);
encrypt_history=True 时核心直接禁用本存储、继续走 JSON(库文件无法整体
DPAPI 加密,不做静默降级)。
"""
import json
import os
import re
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clipboard_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    seq           INTEGER NOT NULL,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    content_type  TEXT    NOT NULL,        -- text | files | image
    content_subtype TEXT,                  -- JWT / Base64 / …(可空,探测缓存)
    content_hash  TEXT,                    -- 去重键(文本=sha1,图片=像素哈希,文件=组合)
    source_app    TEXT,
    source_window TEXT,                    -- 预留(当前恒空)
    text_content  TEXT,                    -- 文本条目全文(FTS 索引)
    files_json    TEXT,                    -- 文件条目路径列表
    rich_json     TEXT,                    -- {"html":…,"rtf":…}
    blob_hash     TEXT,
    blob_mime     TEXT,
    blob_width    INTEGER,
    blob_height   INTEGER,
    blob_size     INTEGER,
    is_pinned     INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_seq   ON clipboard_items(seq DESC);
CREATE INDEX IF NOT EXISTS idx_items_type  ON clipboard_items(content_type);
CREATE INDEX IF NOT EXISTS idx_items_src   ON clipboard_items(source_app);
CREATE INDEX IF NOT EXISTS idx_items_created ON clipboard_items(created_at);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    text_content, content='clipboard_items', content_rowid='id',
    tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON clipboard_items
BEGIN
    INSERT INTO items_fts(rowid, text_content)
    VALUES (new.id, new.text_content);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON clipboard_items
BEGIN
    INSERT INTO items_fts(items_fts, rowid, text_content)
    VALUES ('delete', old.id, old.text_content);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_au AFTER UPDATE OF text_content
ON clipboard_items
BEGIN
    INSERT INTO items_fts(items_fts, rowid, text_content)
    VALUES ('delete', old.id, old.text_content);
    INSERT INTO items_fts(rowid, text_content)
    VALUES (new.id, new.text_content);
END;
"""

_ASCII_ONLY = re.compile(r"^[\x20-\x7e]*$")


def _hash_text(content):
    import hashlib
    return hashlib.sha1(str(content).encode("utf-8", "replace")).hexdigest()


class ClipboardStore:
    """单写者 SQLite 仓库:启动 load_all;运行期 insert/update/delete 写透。"""

    def __init__(self, db_path):
        self.path = str(db_path)
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()
        row = self.db.execute("SELECT COALESCE(MAX(seq), 0) AS m "
                              "FROM clipboard_items").fetchone()
        self._seq = int(row["m"])

    def close(self):
        with self._lock:
            try:
                self.db.close()
            except Exception:
                pass

    # ---- 行 ⇄ 记录 ----
    @staticmethod
    def _rec_from_row(r):
        rec = {"type": r["content_type"], "pinned": bool(r["is_pinned"]),
               "ts": float(r["created_at"]), "_db": r["id"]}
        if r["content_type"] == "text":
            rec["content"] = r["text_content"]
        elif r["content_type"] == "files":
            rec["content"] = json.loads(r["files_json"] or "[]")
        else:
            meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            rec["content"] = meta.get("path") or r["blob_hash"] or ""
            rec["w"], rec["h"] = r["blob_width"] or 0, r["blob_height"] or 0
            rec["hash"] = r["content_hash"] or ""
        if r["source_app"]:
            rec["src"] = r["source_app"]
        rich = json.loads(r["rich_json"]) if r["rich_json"] else {}
        rec.update(rich)
        return rec

    @staticmethod
    def _row_from_rec(rec, seq):
        t = rec.get("type")
        blob_size = None
        try:
            if t == "image":
                blob_size = os.path.getsize(rec.get("content") or "")
        except Exception:
            blob_size = None
        rich = {k: rec[k] for k in ("html", "rtf") if rec.get(k)}
        if t == "text":
            chash, text, files = _hash_text(rec.get("content") or ""), \
                rec.get("content"), None
            bhash, bmime, bw, bh = None, None, None, None
        elif t == "files":
            chash = _hash_text("\n".join(rec.get("content") or []))
            text, files = None, json.dumps(rec.get("content") or [],
                                           ensure_ascii=False)
            bhash, bmime, bw, bh = None, None, None, None
        else:
            chash = rec.get("hash") or ""
            text, files = None, None
            bhash = rec.get("hash") or ""
            bmime = "image/png"
            bw, bh = rec.get("w", 0), rec.get("h", 0)
        now = time.time()
        meta = json.dumps({"path": rec.get("content")},
                          ensure_ascii=False) if t == "image" else None
        return (seq, float(rec.get("ts") or now), now, t,
                rec.get("_subtype"), chash, rec.get("src"),
                None, text, files,
                json.dumps(rich, ensure_ascii=False) if rich else None,
                bhash, bmime, bw, bh, blob_size,
                1 if rec.get("pinned") else 0, meta)

    _COLS = ("seq, created_at, updated_at, content_type, content_subtype, "
             "content_hash, source_app, source_window, text_content, "
             "files_json, rich_json, blob_hash, blob_mime, blob_width, "
             "blob_height, blob_size, is_pinned, metadata_json")
    _PLACE = "(" + ",".join("?" * 18) + ")"

    # ---- 载入 ----
    def load_all(self):
        """按 seq 降序(最近在前)载入全部条目,rec 与现行内存格式一致。"""
        with self._lock:
            rows = self.db.execute(
                "SELECT id, seq, created_at, updated_at, content_type, "
                "content_subtype, content_hash, source_app, source_window, "
                "text_content, files_json, rich_json, blob_hash, blob_mime, "
                "blob_width, blob_height, blob_size, is_pinned, metadata_json "
                "FROM clipboard_items ORDER BY seq DESC").fetchall()
        return [self._rec_from_row(r) for r in rows]

    def count(self):
        with self._lock:
            return int(self.db.execute(
                "SELECT COUNT(*) AS c FROM clipboard_items"
            ).fetchone()["c"])

    # ---- 写透 ----
    def insert(self, rec):
        """插入新条目到最前;rec 原地补 _db 与 _subtype。"""
        with self._lock:
            self._seq += 1
            cur = self.db.execute(
                "INSERT INTO clipboard_items (%s) VALUES %s"
                % (self._COLS, self._PLACE),
                self._row_from_rec(rec, self._seq))
            rec["_db"] = cur.lastrowid
            self.db.commit()
            return rec["_db"]

    def upsert_promote(self, rec):
        """撤销恢复/重复提升:已有 _db 则更新并移到最前,否则插入。"""
        with self._lock:
            dbid = rec.get("_db")
            if dbid is not None and self.db.execute(
                    "SELECT 1 FROM clipboard_items WHERE id = ?", (dbid,)
            ).fetchone():
                self._seq += 1
                row = self._row_from_rec(rec, self._seq)
                self.db.execute(
                    "UPDATE clipboard_items SET seq=?, created_at=?, "
                    "updated_at=?, content_subtype=?, content_hash=?, "
                    "source_app=?, rich_json=?, is_pinned=?, blob_size=? "
                    "WHERE id = ?",
                    (row[0], row[1], row[2], row[4], row[5], row[6],
                     row[10], row[16], row[15], dbid))
                self.db.commit()
                return dbid
            return self.insert(rec)

    def update_pinned(self, dbid, pinned):
        with self._lock:
            self.db.execute(
                "UPDATE clipboard_items SET is_pinned=?, updated_at=? "
                "WHERE id = ?", (1 if pinned else 0, time.time(), dbid))
            self.db.commit()

    def delete(self, dbid):
        with self._lock:
            self.db.execute("DELETE FROM clipboard_items WHERE id = ?",
                            (dbid,))
            self.db.commit()

    def delete_many(self, dbids):
        ids = [int(i) for i in dbids if i is not None]
        if not ids:
            return
        with self._lock:
            self.db.executemany(
                "DELETE FROM clipboard_items WHERE id = ?", [(i,) for i in ids])
            self.db.commit()

    def reset(self):
        """清空全部条目(含 FTS),seq 归零——加密→库模式切换时的整库重建。"""
        with self._lock:
            self.db.execute("DELETE FROM clipboard_items")
            self.db.commit()
            self._seq = 0

    # ---- 查询(未来 UI 分页/筛选的直连通道;本阶段供测试与 API 演进) ----
    def query(self, content_type=None, source_app=None, since=0.0,
              q=None, pinned=None, limit=None, offset=0):
        """参数化组合查询,返回 id 列表(seq 降序)。q 走 FTS/CJK-LIKE。"""
        sql = ("SELECT id FROM clipboard_items WHERE 1=1")
        args = []
        if content_type:
            sql += " AND content_type = ?"
            args.append(content_type)
        if source_app:
            sql += " AND source_app = ?"
            args.append(source_app)
        if since:
            sql += " AND created_at >= ?"
            args.append(float(since))
        if pinned is not None:
            sql += " AND is_pinned = ?"
            args.append(1 if pinned else 0)
        if q:
            sql += " AND id IN (%s)" % self._search_subquery(q)
            args.extend(self._search_args(q))
        sql += " ORDER BY seq DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args.extend([int(limit), int(offset)])
        with self._lock:
            return [r["id"] for r in self.db.execute(sql, args).fetchall()]

    def search(self, q, limit=100):
        """全文搜索:id 列表。ASCII 词走 FTS5;含 CJK 时 FTS 无法分词,
        退回 LIKE(同样参数绑定)。"""
        if not q:
            return []
        sql = "SELECT id FROM clipboard_items WHERE id IN (%s) " \
              "ORDER BY seq DESC LIMIT ?" % self._search_subquery(q)
        args = self._search_args(q) + [int(limit)]
        with self._lock:
            return [r["id"] for r in self.db.execute(sql, args).fetchall()]

    @staticmethod
    def _search_subquery(q):
        if _ASCII_ONLY.match(q):
            return ("SELECT rowid FROM items_fts WHERE items_fts MATCH ?")
        return ("SELECT id FROM clipboard_items WHERE text_content LIKE ?")

    @staticmethod
    def _search_args(q):
        if _ASCII_ONLY.match(q):
            fts_q = " OR ".join('"%s"' % w.replace('"', '""')
                                for w in q.split() if w)
            return [fts_q or q]
        return ["%" + q + "%"]

    def migrate_from_list(self, history):
        """history.json(最近在前)一次性导入,单事务;返回条数。"""
        with self._lock:
            self.db.execute("BEGIN")
            n = 0
            try:
                for rec in history:               # 最旧在后 → 先插旧的,seq 递增
                    self._seq += 1
                    row = self._row_from_rec(rec, self._seq)
                    self.db.execute(
                        "INSERT INTO clipboard_items (%s) VALUES %s"
                        % (self._COLS, self._PLACE), row)
                    n += 1
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            return n
