import sqlite3
from datetime import datetime
from config import CONNECTIONS_DB_PATH


class ConnectionManager:
    def __init__(self):
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(CONNECTIONS_DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tricount_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()

    def add(self, name, token):
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO tricount_connections (name, token) VALUES (?, ?)",
            (name, token)
        )
        conn.commit()
        rec_id = cur.lastrowid
        conn.close()
        return rec_id

    def update(self, rec_id, name=None, token=None):
        conn = self._get_conn()
        fields = []
        values = []
        if name is not None:
            fields.append("name = ?")
            values.append(name)
        if token is not None:
            fields.append("token = ?")
            values.append(token)
        if fields:
            values.append(rec_id)
            conn.execute(
                f"UPDATE tricount_connections SET {', '.join(fields)} WHERE id = ?",
                values
            )
            conn.commit()
        conn.close()

    def delete(self, rec_id):
        conn = self._get_conn()
        conn.execute("DELETE FROM tricount_connections WHERE id = ?", (rec_id,))
        conn.commit()
        conn.close()

    def list_all(self):
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM tricount_connections ORDER BY name"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get(self, rec_id):
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM tricount_connections WHERE id = ?", (rec_id,)
        ).fetchone()
        conn.close()
        if row:
            return dict(row)
        return None
