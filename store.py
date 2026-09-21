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
- 版本迁移:PRAGMA user_version 阶梯;存量库(未打戳)按列形状探测起始
  版本,先补戳再逐级 migrate_N_to_N+1,每级独立 commit,中断可续。

兼容:敏感条目不入本表(私密区 private.json + DPAPI 独立存储);
encrypt_history=True 时核心不启用本存储写透,继续走 JSON。
"""
import json
import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 3   # v1 初版 / v2 +search_text+FTS / v3 +state+deleted_at

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
    metadata_json TEXT,
    state         TEXT    NOT NULL DEFAULT 'active',   -- active | deleted
    deleted_at    REAL                                   -- 进 deleted 态的时刻
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
        self._migrate_schema()     # 版本阶梯:旧库逐级迁到当前(先于建触发器)
        self.db.executescript(_SCHEMA)
        if self._upgraded:
            # 既有行的 FTS 索引重建(外容表只能用 rebuild,不能手工插行)
            self.db.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
        if self._user_version() == 0:
            self._stamp_version(SCHEMA_VERSION)   # 全新库:建表后打当前版本戳
        self.db.commit()
        self._seq = int(self.db.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM clipboard_items"
        ).fetchone()["m"])

    def _user_version(self):
        r = self.db.execute("PRAGMA user_version").fetchone()
        return int(r[0]) if r else 0

    def _stamp_version(self, v):
        """打版本戳。PRAGMA 不支持参数绑定,版本号是封闭集合的整型,
        逐版本写死为字面量(不拼接);新增版本时在此追加一个分支。"""
        if v == 1:
            self.db.execute("PRAGMA user_version = 1")
        elif v == 2:
            self.db.execute("PRAGMA user_version = 2")
        elif v == 3:
            self.db.execute("PRAGMA user_version = 3")
        else:
            raise ValueError("版本 %r 无预写字面量语句" % (v,))
        self.db.commit()

    def _sniff_legacy_version(self):
        """未打戳的存量库按列形状定起始版本(v1 无 search_text;v2 无 state)。"""
        names = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'clipboard_items'").fetchall()
        if not names:
            return 0                                  # 全新库
        cols = {r["name"] for r in self.db.execute(
            "PRAGMA table_info(clipboard_items)").fetchall()}
        if "search_text" not in cols:
            return 1
        if "state" not in cols:
            return 2
        return SCHEMA_VERSION

    def _migrate_schema(self):
        """版本阶梯迁移(PRAGMA user_version 随事务原子落盘)。

        存量库未打戳(user_version=0)→先按列形状探测起始版本并补戳,
        再逐级 migrate;每级迁移独立 commit+版本戳,中断后重开从戳处继续。
        高于当前支持的版本直接拒绝打开——旧代码写新库是数据损坏路径。"""
        self._upgraded = False
        v = self._user_version()
        if v == 0:
            v = self._sniff_legacy_version()
            if v == 0:
                return                                # 全新库:建表后统一打戳
            self._stamp_version(v)                    # 先补起始戳再逐级升
        if v > SCHEMA_VERSION:
            raise ValueError(
                "clipboard.db schema v%d 高于本版本支持的 v%d,请先升级应用"
                % (v, SCHEMA_VERSION))
        while v < SCHEMA_VERSION:
            self._MIGRATIONS[v](self)
            v += 1
            self._stamp_version(v)

    def _migrate_1_to_2(self):
        """v1→v2:加 search_text 列并回填。必须在 executescript(_SCHEMA)之前
        ——schema 里的触发器引用 search_text,而外容 FTS 绝不能经触发器
        手工回填(索引会损坏)。v1 可能带着按 text_content 建的旧 FTS/触发器:
        清场,稍后由 _SCHEMA 重建。"""
        cols = [r["name"] for r in self.db.execute(
            "PRAGMA table_info(clipboard_items)").fetchall()]
        if "search_text" in cols:
            return
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
        self.db.execute("DROP TRIGGER IF EXISTS items_fts_ai")
        self.db.execute("DROP TRIGGER IF EXISTS items_fts_ad")
        self.db.execute("DROP TRIGGER IF EXISTS items_fts_au")
        self.db.execute("DROP TABLE IF EXISTS items_fts")
        self._upgraded = True

    def _migrate_2_to_3(self):
        """v2→v3:补 state/deleted_at 列(带默认值,老行视为 active)。"""
        cols = {r["name"] for r in self.db.execute(
            "PRAGMA table_info(clipboard_items)").fetchall()}
        if "state" not in cols:
            self.db.execute("ALTER TABLE clipboard_items "
                            "ADD COLUMN state TEXT NOT NULL DEFAULT 'active'")
        if "deleted_at" not in cols:
            self.db.execute("ALTER TABLE clipboard_items "
                            "ADD COLUMN deleted_at REAL")

    # 版本阶梯:v→v+1 迁移函数表;新版本 = 追加一级 + SCHEMA_VERSION 递增
    _MIGRATIONS = {1: _migrate_1_to_2, 2: _migrate_2_to_3}

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
        meta = None
        if t == "image":
            meta = json.dumps({"path": rec.get("content")},
                              ensure_ascii=False)
        elif rec.get("type_override"):
            meta = json.dumps({"type_override": rec["type_override"]},
                              ensure_ascii=False)
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
        if r["metadata_json"]:
            _meta = json.loads(r["metadata_json"])
            if _meta.get("type_override"):
                rec["type_override"] = _meta["type_override"]
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
                "metadata_json FROM clipboard_items WHERE state='active' "
                "ORDER BY seq DESC"
            ).fetchall()
        return [self._rec_from_row(r) for r in rows]

    def count(self):
        with self._lock:
            return int(self.db.execute(
                "SELECT COUNT(*) AS c FROM clipboard_items "
                "WHERE state='active'"
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
        """撤销恢复/重复提升:已有 _db 则置回 active 并移到最前,否则插入。
        行已被 GC 时走插入重建——数据在内存 rec 里仍然完整。"""
        with self._lock:
            dbid = rec.get("_db")
            if dbid is not None and self.db.execute(
                    "SELECT 1 FROM clipboard_items WHERE id = ?", (dbid,)
            ).fetchone():
                self._seq += 1
                row = self._row_from_rec(rec, self._seq)
                self.db.execute(
                    "UPDATE clipboard_items SET seq=?, created_at=?, "
                    "updated_at=?, state='active', deleted_at=NULL, "
                    "content_subtype=?, content_hash=?, "
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

    def mark_deleted(self, dbids, now=None):
        """用户删除:行置 deleted 态(可撤销);不物理删行,不动 blob 文件。"""
        ids = [int(i) for i in dbids if i is not None]
        if not ids:
            return
        now = float(now if now is not None else time.time())
        with self._lock:
            self.db.executemany(
                "UPDATE clipboard_items SET state='deleted', deleted_at=?, "
                "updated_at=? WHERE id = ?",
                [(now, now, i) for i in ids])
            self.db.commit()

    def restore(self, dbids):
        """撤销删除:行回 active 态并提升 seq 到最前(列表语义 = 最近)。"""
        ids = [int(i) for i in dbids if i is not None]
        if not ids:
            return
        with self._lock:
            for i in ids:
                self._seq += 1
                self.db.execute(
                    "UPDATE clipboard_items SET state='active', deleted_at=NULL, "
                    "seq=? WHERE id = ?", (self._seq, i))
            self.db.commit()

    def collect_garbage(self, grace_seconds):
        """物理删除宽限期已过的 deleted 行;返回 [(id, blob_hash), …] 供
        上层做 blob 引用计数清点(grace 内的行保留——撤销还救得回)。"""
        cutoff = time.time() - float(grace_seconds)
        with self._lock:
            rows = self.db.execute(
                "SELECT id, blob_hash FROM clipboard_items "
                "WHERE state='deleted' AND deleted_at IS NOT NULL "
                "AND deleted_at < ?", (cutoff,)).fetchall()
            if not rows:
                return []
            self.db.executemany(
                "DELETE FROM clipboard_items WHERE id = ?",
                [(r["id"],) for r in rows])
            self.db.commit()
            return [(r["id"], r["blob_hash"]) for r in rows]

    def blob_refcount(self, blob_hash):
        """该 blob 被表内多少行引用(任意态——deleted 行在宽限期内仍算,
        撤销后引用必须仍然成立);文件是否可删由调用方据此判断。"""
        with self._lock:
            return int(self.db.execute(
                "SELECT COUNT(*) AS c FROM clipboard_items "
                "WHERE blob_hash = ?", (blob_hash,)).fetchone()["c"])

    # ---- 诊断/维护(只读检查 + 索引重建;诊断页与健康检查共用) ----
    def integrity_check(self):
        """PRAGMA integrity_check 结论(ok = 数据库文件结构完好)。"""
        with self._lock:
            r = self.db.execute("PRAGMA integrity_check").fetchone()
            return str(r[0]) if r else "unknown"

    def deleted_count(self):
        """deleted 态行数(宽限期内待 GC,撤销仍可救回)。"""
        with self._lock:
            return int(self.db.execute(
                "SELECT COUNT(*) AS c FROM clipboard_items "
                "WHERE state = 'deleted'").fetchone()["c"])

    def image_blob_refs(self):
        """全部图片行(任意态)的 (blob_hash, 记录路径) 列表——引用存在性
        与孤儿清点的事实来源;宽限期内 deleted 行同样要求 blob 在。"""
        with self._lock:
            rows = self.db.execute(
                "SELECT blob_hash, metadata_json FROM clipboard_items "
                "WHERE content_type = 'image'").fetchall()
        out = []
        for r in rows:
            path = None
            try:
                meta = json.loads(r["metadata_json"]) \
                    if r["metadata_json"] else {}
                path = meta.get("path")
            except Exception:
                path = None
            out.append((r["blob_hash"] or "", str(path or "")))
        return out

    def rebuild_fts(self):
        """全文索引整体重建(诊断页维护动作;外容表只能 rebuild)。"""
        with self._lock:
            self.db.execute(
                "INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
            self.db.commit()

    def update_image_ref(self, dbid, blob_hash, path):
        """CAS 迁移:图片条目的 blob 落位到分片路径后的引用更新。"""
        with self._lock:
            self.db.execute(
                "UPDATE clipboard_items SET blob_hash=?, metadata_json=?, "
                "updated_at=? WHERE id = ?",
                (blob_hash, json.dumps({"path": path}, ensure_ascii=False),
                 time.time(), int(dbid)))
            self.db.commit()

    def set_type_override(self, dbid, value):
        """用户类型纠正持久化:合并写入 metadata_json(value 为 None 清除)。"""
        with self._lock:
            row = self.db.execute(
                "SELECT metadata_json FROM clipboard_items WHERE id = ?",
                (int(dbid),)).fetchone()
            meta = json.loads(row["metadata_json"]) if row and \
                row["metadata_json"] else {}
            if value:
                meta["type_override"] = value
            else:
                meta.pop("type_override", None)
            self.db.execute(
                "UPDATE clipboard_items SET metadata_json=?, updated_at=? "
                "WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False) if meta else None,
                 time.time(), int(dbid)))
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
              q=None, pinned=None, until=None, limit=None, offset=0):
        """参数化组合查询,返回 id 列表(seq 降序)。q 为子串语义(LIKE);
        until 为时间上界(created_at < until,None 不过滤)。"""
        u = None if until is None else float(until)
        a = self._filter_args(content_type, source_app, since, q,
                              pinned) + (u, u)
        sql = ("SELECT id FROM clipboard_items WHERE state='active' "
               "AND (? IS NULL OR content_type = ?) "
               "AND (? IS NULL OR source_app = ?) "
               "AND (? = 0 OR created_at >= ?) "
               "AND (? IS NULL OR is_pinned = ?) "
               "AND (? IS NULL OR search_text LIKE ?) "
               "AND (? IS NULL OR created_at < ?) "
               "ORDER BY seq DESC LIMIT ? OFFSET ?")
        args = a + (10 ** 9 if limit is None else int(limit), int(offset))
        with self._lock:
            return [r["id"] for r in self.db.execute(sql, args).fetchall()]

    def query_page(self, content_type=None, source_app=None, since=0.0,
                   q=None, pinned=None, until=None, limit=200, offset=0):
        """分页查询:返回 (rec 列表, 命中总数)。rec 与内存条目同构(含 _db)。
        until 为时间上界(created_at < until,None 不过滤)。"""
        u = None if until is None else float(until)
        a = self._filter_args(content_type, source_app, since, q,
                              pinned) + (u, u)
        total_sql = ("SELECT COUNT(*) AS c FROM clipboard_items "
                     "WHERE state='active' "
                     "AND (? IS NULL OR content_type = ?) "
                     "AND (? IS NULL OR source_app = ?) "
                     "AND (? = 0 OR created_at >= ?) "
                     "AND (? IS NULL OR is_pinned = ?) "
                     "AND (? IS NULL OR search_text LIKE ?) "
                     "AND (? IS NULL OR created_at < ?)")
        page_sql = ("SELECT id, seq, created_at, updated_at, content_type, "
                    "content_subtype, content_hash, source_app, source_window, "
                    "text_content, search_text, files_json, rich_json, "
                    "blob_hash, blob_mime, blob_width, blob_height, blob_size, "
                    "is_pinned, metadata_json FROM clipboard_items "
                    "WHERE state='active' "
                    "AND (? IS NULL OR content_type = ?) "
                    "AND (? IS NULL OR source_app = ?) "
                    "AND (? = 0 OR created_at >= ?) "
                    "AND (? IS NULL OR is_pinned = ?) "
                    "AND (? IS NULL OR search_text LIKE ?) "
                    "AND (? IS NULL OR created_at < ?) "
                    "ORDER BY seq DESC LIMIT ? OFFSET ?")
        with self._lock:
            total = int(self.db.execute(total_sql, a).fetchone()["c"])
            rows = self.db.execute(
                page_sql, a + (int(limit), int(offset))).fetchall()
        return [self._rec_from_row(r) for r in rows], total

    def search(self, q, limit=100):
        """搜索(active 行)id 列表(seq 降序):子串语义,等价于
        query(q=…, limit=…)——FTS 词级索引由触发器维护,供将来切换。"""
        if not q:
            return []
        return self.query(q=q, limit=limit)

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
