import sqlite3
import json
import calendar
from datetime import datetime, date, timedelta
from config import RECURRING_DB_PATH


class RecurringManager:
    def __init__(self):
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(RECURRING_DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recurring_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tricount_token TEXT NOT NULL,
                description TEXT NOT NULL,
                amount REAL NOT NULL,
                payer_uuid TEXT NOT NULL,
                split_mode TEXT NOT NULL DEFAULT 'equal',
                split_data TEXT,
                category TEXT,
                frequency TEXT NOT NULL,
                interval_count INTEGER NOT NULL DEFAULT 1,
                day_of_week INTEGER,
                day_of_month INTEGER,
                next_run_date TEXT NOT NULL,
                last_run_date TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Migration: add columns if missing (existing DB)
        for col in ("day_of_week", "day_of_month", "split_members"):
            try:
                conn.execute(f"ALTER TABLE recurring_expenses ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recurring_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recurring_id INTEGER NOT NULL,
                transaction_id TEXT,
                executed_at TEXT NOT NULL DEFAULT (datetime('now')),
                status TEXT NOT NULL DEFAULT 'success',
                error_message TEXT,
                FOREIGN KEY (recurring_id) REFERENCES recurring_expenses(id)
            )
        """)
        conn.commit()
        conn.close()

    def add_recurring(self, tricount_token, description, amount, payer_uuid,
                      frequency, interval_count=1, split_mode='equal',
                      split_data=None, category=None, start_date=None,
                      day_of_week=None, day_of_month=None,
                      split_members=None):
        conn = self._get_conn()
        if start_date is None:
            start_date = date.today().isoformat()
        next_run = self.compute_first_run(
            frequency, interval_count, start_date,
            day_of_week=day_of_week, day_of_month=day_of_month
        )
        cur = conn.execute("""
            INSERT INTO recurring_expenses
                (tricount_token, description, amount, payer_uuid,
                 split_mode, split_data, split_members, category, frequency,
                 interval_count, day_of_week, day_of_month, next_run_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            tricount_token, description, amount, payer_uuid,
            split_mode, json.dumps(split_data) if split_data else None,
            json.dumps(split_members) if split_members else None,
            category, frequency, interval_count,
            day_of_week, day_of_month, next_run
        ))
        conn.commit()
        rec_id = cur.lastrowid
        conn.close()
        return rec_id

    def get_recurring(self, rec_id):
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM recurring_expenses WHERE id = ?", (rec_id,)
        ).fetchone()
        conn.close()
        if row:
            d = dict(row)
            if d.get("split_data"):
                d["split_data"] = json.loads(d["split_data"])
            if d.get("split_members"):
                d["split_members"] = json.loads(d["split_members"])
            return d
        return None

    def list_recurring(self, tricount_token=None, active_only=True):
        conn = self._get_conn()
        if tricount_token:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM recurring_expenses WHERE tricount_token = ? AND is_active = 1 ORDER BY next_run_date",
                    (tricount_token,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM recurring_expenses WHERE tricount_token = ? ORDER BY next_run_date",
                    (tricount_token,)
                ).fetchall()
        else:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM recurring_expenses WHERE is_active = 1 ORDER BY next_run_date"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM recurring_expenses ORDER BY next_run_date"
                ).fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("split_data"):
                d["split_data"] = json.loads(d["split_data"])
            if d.get("split_members"):
                d["split_members"] = json.loads(d["split_members"])
            result.append(d)
        return result

    def update_recurring(self, rec_id, **kwargs):
        allowed = ["description", "amount", "payer_uuid", "split_mode",
                   "split_data", "split_members", "category", "frequency",
                   "interval_count", "day_of_week", "day_of_month",
                   "next_run_date", "is_active"]
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        if "split_data" in fields and fields["split_data"] is not None:
            fields["split_data"] = json.dumps(fields["split_data"])
        if "split_members" in fields and fields["split_members"] is not None:
            fields["split_members"] = json.dumps(fields["split_members"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [rec_id]
        conn = self._get_conn()
        conn.execute(f"UPDATE recurring_expenses SET {sets} WHERE id = ?", values)
        conn.commit()
        conn.close()
        return True

    def delete_recurring(self, rec_id):
        conn = self._get_conn()
        conn.execute("DELETE FROM recurring_expenses WHERE id = ?", (rec_id,))
        conn.execute("DELETE FROM recurring_log WHERE recurring_id = ?", (rec_id,))
        conn.commit()
        conn.close()

    def _next_weekday(self, from_date, day_of_week, interval_weeks=1):
        days_ahead = day_of_week - from_date.weekday()
        if days_ahead <= 0:
            days_ahead += 7 * interval_weeks
        return from_date + timedelta(days=days_ahead)

    def _next_day_of_month(self, from_date, day_of_month, interval_months=1):
        month = from_date.month + interval_months
        year = from_date.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        last = calendar.monthrange(year, month)[1]
        return date(year, month, min(day_of_month, last))

    def compute_first_run(self, frequency, interval_count, start_date,
                          day_of_week=None, day_of_month=None):
        d = datetime.strptime(start_date, "%Y-%m-%d").date()
        if frequency == "daily":
            return d.isoformat()
        if frequency in ("weekly", "biweekly") and day_of_week is not None:
            iw = interval_count * (2 if frequency == "biweekly" else 1)
            return self._next_weekday(d, day_of_week, iw).isoformat()
        if frequency == "monthly" and day_of_month is not None:
            if d.day <= day_of_month:
                try:
                    return d.replace(day=day_of_month).isoformat()
                except ValueError:
                    last = calendar.monthrange(d.year, d.month)[1]
                    return d.replace(day=last).isoformat()
            return self._next_day_of_month(d, day_of_month, 1).isoformat()
        return d.isoformat()

    def compute_next_run(self, frequency, interval_count, from_date=None,
                         day_of_week=None, day_of_month=None):
        if from_date is None:
            from_date = date.today()
        else:
            from_date = datetime.strptime(from_date, "%Y-%m-%d").date()

        if frequency == "daily":
            return (from_date + timedelta(days=interval_count)).isoformat()

        if frequency in ("weekly", "biweekly"):
            if day_of_week is not None:
                iw = interval_count * (2 if frequency == "biweekly" else 1)
                return self._next_weekday(from_date, day_of_week, iw).isoformat()
            weeks = interval_count * (2 if frequency == "biweekly" else 1)
            return (from_date + timedelta(weeks=weeks)).isoformat()

        if frequency == "monthly":
            if day_of_month is not None:
                return self._next_day_of_month(from_date, day_of_month, interval_count).isoformat()
            month = from_date.month + interval_count
            year = from_date.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            day = min(from_date.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
            return date(year, month, day).isoformat()

        return from_date.isoformat()

    def get_due(self, tricount_token):
        conn = self._get_conn()
        today = date.today().isoformat()
        rows = conn.execute(
            "SELECT * FROM recurring_expenses WHERE tricount_token = ? AND is_active = 1 AND next_run_date <= ?",
            (tricount_token, today)
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("split_data"):
                d["split_data"] = json.loads(d["split_data"])
            if d.get("split_members"):
                d["split_members"] = json.loads(d["split_members"])
            result.append(d)
        return result

    def mark_executed(self, rec_id, next_run_date, transaction_id=None, error=None):
        conn = self._get_conn()
        today = date.today().isoformat()
        if error:
            conn.execute(
                "INSERT INTO recurring_log (recurring_id, status, error_message) VALUES (?, 'error', ?)",
                (rec_id, error)
            )
            conn.execute(
                "UPDATE recurring_expenses SET next_run_date = ?, last_run_date = ? WHERE id = ?",
                (next_run_date, today, rec_id)
            )
        else:
            conn.execute(
                "INSERT INTO recurring_log (recurring_id, transaction_id) VALUES (?, ?)",
                (rec_id, transaction_id)
            )
            conn.execute(
                "UPDATE recurring_expenses SET next_run_date = ?, last_run_date = ? WHERE id = ?",
                (next_run_date, today, rec_id)
            )
        conn.commit()
        conn.close()

    def get_all_tokens(self):
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT DISTINCT tricount_token FROM recurring_expenses WHERE is_active = 1"
        ).fetchall()
        conn.close()
        return [r["tricount_token"] for r in rows]

    def get_logs(self, rec_id=None, limit=50):
        conn = self._get_conn()
        if rec_id:
            rows = conn.execute(
                "SELECT * FROM recurring_log WHERE recurring_id = ? ORDER BY executed_at DESC LIMIT ?",
                (rec_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM recurring_log ORDER BY executed_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
