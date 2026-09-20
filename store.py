# -*- coding: utf-8 -*-
"""SQLite 历史存储(clipboard.db)—— 剪贴板条目的事实存储。

设计:
- 表 clipboard_items + seq 显式顺序列(列表语义 = 最近在前;dedup 提升
  与撤销恢复都取 max(seq)+1)。
- 统一检索串 search_text 列(文本=内容 / 文件=路径 / 图片=尺寸行),
  FTS5(unicode61)对其建全文索引;子串搜索(UI 语义)走 LIKE 参数绑定。
- 图片二进制不入库:blob_hash 等元数据列指向磁盘上按内容寻址的文件
  (images/img_<hash>.png 与 data/blobs/<2位>/<hash> 两种布局并存,
  记录用绝对路径引用,互不冲突)。
- 安全约定:所有 SQL 为调用点内联字面量,动态值一律参数绑定;
  组合过滤用固定语句 + 空参数(? IS NULL OR col = ?),不拼接 SQL 文本。
- 写入策略:单写者(GUI 线程)逐操作写透,WAL 模式。

兼容:敏感条目不入本表(私密区 private.json + DPAPI 独立存储);
encrypt_history=True 时核心不启用本存储写透,继续走 JSON。
"""
import json
import os
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clipboard_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    seq           INTEGER NOT NULL,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    content_type  TEXT    NOT NULL,
    content_subtype TEXT,
    content_hash  TEXT,
    source_app    TEXT,
    source_window TEXT,
    text_content  TEXT,
    search_text   TEXT,
    files_json    TEXT,
    rich_json     TEXT,
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
    search_text, content='clipboard_items', content_rowid='id',
    tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON clipboard_items
BEGIN
    INSERT INTO items_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON clipboard_items
BEGIN
    INSERT INTO items_fts(items_fts, rowid, search_text)
    VALUES ('delete', old.id, old.search_text);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_au AFTER UPDATE OF search_text
ON clipboard_items
BEGIN
    INSERT INTO items_fts(items_fts, rowid, search_text)
    VALUES ('delete', old.id, old.search_text);
    INSERT INTO items_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;
"""


def _hash_text(content):
    import hashlib
    return hashlib.sha256(str(content).encode("utf-8", "replace")).hexdigest()


def blob_full_path(app_dir, hsh):
    """新布局的 blob 路径:data/blobs/<前两位>/<哈希>(按内容寻址,分片目录)。"""
    h = str(hsh or "")
    return os.path.join(app_dir, "data", "blobs", h[:2] if len(h) >= 2 else "_", h)


class ClipboardStore:
    """单写者 SQLite 仓库:启动 load_all;运行期 insert/update/delete 写透。

    所有 execute() 的 SQL 均为内联字面量,动态值全部参数绑定;
    组合过滤用固定语句 + 空参数(? IS NULL OR …),不存在 SQL 文本拼接。"""

    def __init__(self, db_path):
        self.path = str(db_path)
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._ensure_v2()          # 旧库升级必须先于 schema(触发器引用新列)
        self.db.executescript(_SCHEMA)
        if self._upgraded:
            # 既有行的 FTS 索引重建(外容表只能用 rebuild,不能手工插行)
            self.db.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
        self.db.commit()
        self._seq = int(self.db.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM clipboard_items"
        ).fetchone()["m"])

    def _ensure_v2(self):
        """v1 库(无 search_text)就地升级:加列、回填。
        必须在 executescript(_SCHEMA) 之前执行——schema 里的触发器引用
        search_text,而外容 FTS 绝不能经触发器手工回填(索引会损坏)。"""
        self._upgraded = False
        names = [r["name"] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'clipboard_items'").fetchall()]
        if not names:
            return                                  # 全新库,直接建 v2
        cols = [r["name"] for r in self.db.execute(
            "PRAGMA table_info(clipboard_items)").fetchall()]
        if "search_text" in cols:
            return                                  # 已是 v2
        self.db.execute("ALTER TABLE clipboard_items ADD COLUMN search_text TEXT")
        rows = self.db.execute(
            "SELECT id, content_type, text_content, files_json, "
            "blob_width, blob_height FROM clipboard_items").fetchall()
        for r in rows:
            if r["content_type"] == "files":
                files = json.loads(r["files_json"] or "[]")
                st = (os.path.basename(files[0]) + " " + " ".join(files)) \
                    if files else ""
            elif r["content_type"] == "image":
                st = "图片 截图 %s×%s" % (r["blob_width"] or 0,
                                         r["blob_height"] or 0)
            else:
                st = r["text_content"] or ""
            self.db.execute(
                "UPDATE clipboard_items SET search_text = ? WHERE id = ?",
                (st, r["id"]))
        # v1 可能带着按 text_content 建的旧 FTS/触发器:清场,稍后重建
        self.db.execute("DROP TRIGGER IF EXISTS items_fts_ai")
        self.db.execute("DROP TRIGGER IF EXISTS items_fts_ad")
        self.db.execute("DROP TRIGGER IF EXISTS items_fts_au")
        self.db.execute("DROP TABLE IF EXISTS items_fts")
        self._upgraded = True

    def close(self):
        with self._lock:
            try:
                self.db.close()
            except Exception:
                pass

    # ---- 行 ⇄ 记录 ----
    @staticmethod
    def _search_text_of(rec):
        """统一检索串(与内存版 core._search_text 语义一致)。"""
        t = rec.get("type")
        if t == "files":
            c = rec.get("content") or []
            return ((os.path.basename(c[0]) + " " + " ".join(c)) if c else "")
        if t == "image":
            return "图片 截图 %s×%s" % (rec.get("w", 0), rec.get("h", 0))
        return str(rec.get("content") or "")

    @classmethod
    def _row_from_rec(cls, rec, seq):
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
                None, text, cls._search_text_of(rec), files,
                json.dumps(rich, ensure_ascii=False) if rich else None,
                bhash, bmime, bw, bh, blob_size,
                1 if rec.get("pinned") else 0, meta)

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
        if r["content_subtype"]:
            rec["_subtype"] = r["content_subtype"]
        rich = json.loads(r["rich_json"]) if r["rich_json"] else {}
        rec.update(rich)
        return rec

    # ---- 载入 ----
    def load_all(self):
        """按 seq 降序(最近在前)载入全部条目,rec 与现行内存格式一致。"""
        with self._lock:
            rows = self.db.execute(
                "SELECT id, seq, created_at, updated_at, content_type, "
                "content_subtype, content_hash, source_app, source_window, "
                "text_content, search_text, files_json, rich_json, blob_hash, "
                "blob_mime, blob_width, blob_height, blob_size, is_pinned, "
                "metadata_json FROM clipboard_items ORDER BY seq DESC"
            ).fetchall()
        return [self._rec_from_row(r) for r in rows]

    def count(self):
        with self._lock:
            return int(self.db.execute(
                "SELECT COUNT(*) AS c FROM clipboard_items"
            ).fetchone()["c"])

    # ---- 写透 ----
    def insert(self, rec):
        """插入新条目到最前;rec 原地补 _db。"""
        with self._lock:
            self._seq += 1
            cur = self.db.execute(
                "INSERT INTO clipboard_items (seq, created_at, updated_at, "
                "content_type, content_subtype, content_hash, source_app, "
                "source_window, text_content, search_text, files_json, "
                "rich_json, blob_hash, blob_mime, blob_width, blob_height, "
                "blob_size, is_pinned, metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    "source_app=?, search_text=?, rich_json=?, blob_size=?, "
                    "is_pinned=? WHERE id = ?",
                    (row[0], row[1], row[2], row[4], row[5], row[6],
                     row[9], row[11], row[16], row[17], dbid))
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

    # ---- 查询(UI 视图直连通道;固定语句 + 空参数,不拼接 SQL 文本) ----
    def _filter_args(self, content_type, source_app, since, q, pinned):
        qlike = None
        if q:
            qlike = "%" + str(q).strip().casefold() + "%"
        p = None if pinned is None else (1 if pinned else 0)
        return (content_type, content_type, source_app, source_app,
                float(since or 0.0), float(since or 0.0),
                p, p, qlike, qlike)

    def query(self, content_type=None, source_app=None, since=0.0,
              q=None, pinned=None, limit=None, offset=0):
        """参数化组合查询,返回 id 列表(seq 降序)。q 为子串语义(LIKE)。"""
        a = self._filter_args(content_type, source_app, since, q, pinned)
        sql = ("SELECT id FROM clipboard_items WHERE "
               "(? IS NULL OR content_type = ?) "
               "AND (? IS NULL OR source_app = ?) "
               "AND (? = 0 OR created_at >= ?) "
               "AND (? IS NULL OR is_pinned = ?) "
               "AND (? IS NULL OR search_text LIKE ?) "
               "ORDER BY seq DESC LIMIT ? OFFSET ?")
        args = a + (10 ** 9 if limit is None else int(limit), int(offset))
        with self._lock:
            return [r["id"] for r in self.db.execute(sql, args).fetchall()]

    def query_page(self, content_type=None, source_app=None, since=0.0,
                   q=None, pinned=None, limit=200, offset=0):
        """分页查询:返回 (rec 列表, 命中总数)。rec 与内存条目同构(含 _db)。"""
        a = self._filter_args(content_type, source_app, since, q, pinned)
        total_sql = ("SELECT COUNT(*) AS c FROM clipboard_items WHERE "
                     "(? IS NULL OR content_type = ?) "
                     "AND (? IS NULL OR source_app = ?) "
                     "AND (? = 0 OR created_at >= ?) "
                     "AND (? IS NULL OR is_pinned = ?) "
                     "AND (? IS NULL OR search_text LIKE ?)")
        page_sql = ("SELECT id, seq, created_at, updated_at, content_type, "
                    "content_subtype, content_hash, source_app, source_window, "
                    "text_content, search_text, files_json, rich_json, "
                    "blob_hash, blob_mime, blob_width, blob_height, blob_size, "
                    "is_pinned, metadata_json FROM clipboard_items WHERE "
                    "(? IS NULL OR content_type = ?) "
                    "AND (? IS NULL OR source_app = ?) "
                    "AND (? = 0 OR created_at >= ?) "
                    "AND (? IS NULL OR is_pinned = ?) "
                    "AND (? IS NULL OR search_text LIKE ?) "
                    "ORDER BY seq DESC LIMIT ? OFFSET ?")
        with self._lock:
            total = int(self.db.execute(total_sql, a).fetchone()["c"])
            rows = self.db.execute(
                page_sql, a + (int(limit), int(offset))).fetchall()
        return [self._rec_from_row(r) for r in rows], total

    def migrate_from_list(self, history):
        """history 列表(最旧在前)一次性导入,单事务;返回条数。"""
        with self._lock:
            self.db.execute("BEGIN")
            n = 0
            try:
                for rec in history:
                    self._seq += 1
                    self.db.execute(
                        "INSERT INTO clipboard_items (seq, created_at, "
                        "updated_at, content_type, content_subtype, "
                        "content_hash, source_app, source_window, text_content, "
                        "search_text, files_json, rich_json, blob_hash, "
                        "blob_mime, blob_width, blob_height, blob_size, "
                        "is_pinned, metadata_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        self._row_from_rec(rec, self._seq))
                    n += 1
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            return n
