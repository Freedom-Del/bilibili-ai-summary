"""
SQLite 数据库模块 — 历史记录持久化
零依赖，Python 标准库 sqlite3
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")


def _get_conn() -> sqlite3.Connection:
    """获取连接，启用 WAL 模式以支持并发读写"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 返回字典式行
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """初始化数据库表（幂等）"""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                bvid        TEXT    NOT NULL,
                title       TEXT    NOT NULL,
                url         TEXT    NOT NULL,
                markdown    TEXT    NOT NULL,
                char_count  INTEGER DEFAULT 0,
                method      TEXT    DEFAULT '',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 索引：按 BV 号快速查找是否已总结过
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_bvid ON history(bvid)
        """)
        # 兼容旧库：添加 sort_order 列
        cols = [r[1] for r in conn.execute("PRAGMA table_info(history)").fetchall()]
        if "sort_order" not in cols:
            conn.execute("ALTER TABLE history ADD COLUMN sort_order REAL NOT NULL DEFAULT 0")
        if "short_title" not in cols:
            conn.execute("ALTER TABLE history ADD COLUMN short_title TEXT NOT NULL DEFAULT ''")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_sort ON history(sort_order, id)
        """)


def save_summary(bvid: str, title: str, url: str, markdown: str,
                 char_count: int = 0, method: str = "",
                 short_title: str = "") -> int:
    """保存总结，返回新记录的 ID"""
    with _get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO history (bvid, title, url, markdown, char_count, method, short_title)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (bvid, title, url, markdown, char_count, method, short_title),
        )
        return cursor.lastrowid


def get_history(limit: int = 20, offset: int = 0) -> list[dict]:
    """获取历史列表（不含 markdown 正文，减少传输量）"""
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT id, bvid, title, short_title, url, char_count, method, created_at, sort_order
               FROM history
               ORDER BY sort_order, id DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_history_count() -> int:
    """历史总数"""
    with _get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]


def get_by_id(sid: int) -> dict | None:
    """根据 ID 获取完整记录（含 markdown）"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM history WHERE id = ?", (sid,)
        ).fetchone()
        return dict(row) if row else None


def get_by_bvid(bvid: str) -> dict | None:
    """检查某个 BV 号是否已总结过"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, created_at FROM history WHERE bvid = ? ORDER BY id DESC LIMIT 1",
            (bvid,),
        ).fetchone()
        return dict(row) if row else None


def delete_history(sid: int) -> bool:
    """删除一条历史"""
    with _get_conn() as conn:
        cursor = conn.execute("DELETE FROM history WHERE id = ?", (sid,))
        return cursor.rowcount > 0


def update_title(sid: int, new_title: str) -> dict | None:
    """更新标题，同时替换 markdown 第一行 # 标题"""
    with _get_conn() as conn:
        row = conn.execute("SELECT title, markdown FROM history WHERE id = ?", (sid,)).fetchone()
        if not row:
            return None
        old_title = row["title"]
        markdown = row["markdown"]
        # 替换 markdown 第一行 # 旧标题 → # 新标题
        updated_md = markdown.replace(f"# {old_title}", f"# {new_title}", 1)
        conn.execute(
            "UPDATE history SET title = ?, markdown = ? WHERE id = ?",
            (new_title, updated_md, sid),
        )
        return dict(conn.execute("SELECT * FROM history WHERE id = ?", (sid,)).fetchone())


def update_sort_orders(orders: list[dict]) -> None:
    """批量更新排序值，orders 为 [{"id": 1, "sort_order": 0.0}, ...]"""
    with _get_conn() as conn:
        conn.executemany(
            "UPDATE history SET sort_order = ? WHERE id = ?",
            [(o["sort_order"], o["id"]) for o in orders],
        )


def update_short_title(sid: int, new_short_title: str) -> dict | None:
    """更新短标题"""
    with _get_conn() as conn:
        row = conn.execute("SELECT id FROM history WHERE id = ?", (sid,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE history SET short_title = ? WHERE id = ?",
                     (new_short_title, sid))
        return dict(conn.execute("SELECT * FROM history WHERE id = ?", (sid,)).fetchone())
