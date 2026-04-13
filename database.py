"""
database.py - SQLiteデータベース管理モジュール

タスク・メンバー・会話状態のCRUD操作を提供する。
"""

import sqlite3
import json
from datetime import date, datetime
from typing import Optional


# データベースファイルのパス
DB_PATH = "tasks.db"


def get_connection() -> sqlite3.Connection:
    """データベース接続を取得する。

    Returns:
        sqlite3.Connection: データベース接続オブジェクト
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 結果を辞書形式で取得できるようにする
    return conn


def init_db() -> None:
    """データベースとテーブルを初期化する。

    テーブルが存在しない場合のみ作成する。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # メンバーテーブル
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                line_user_id TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                group_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # タスクテーブル
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                assignee_id TEXT NOT NULL,
                due_date DATE NOT NULL,
                status TEXT DEFAULT 'pending',
                group_id TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)

        # 会話状態テーブル（タスク追加・変更の途中状態を保持）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_states (
                user_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                temp_data TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────
# メンバー操作
# ─────────────────────────────────────

def add_member(line_user_id: str, display_name: str, group_id: str) -> None:
    """メンバーを登録する（既に存在する場合は表示名を更新）。

    Args:
        line_user_id: LINEユーザーID
        display_name: 表示名
        group_id: グループID
    """
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO members (line_user_id, display_name, group_id)
            VALUES (?, ?, ?)
            ON CONFLICT(line_user_id) DO UPDATE SET display_name = excluded.display_name
        """, (line_user_id, display_name, group_id))
        conn.commit()
    finally:
        conn.close()


def get_members(group_id: str) -> list[dict]:
    """グループに所属するメンバー一覧を取得する。

    Args:
        group_id: グループID

    Returns:
        メンバー情報のリスト（line_user_id, display_name を含む）
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT line_user_id, display_name FROM members WHERE group_id = ? ORDER BY display_name",
            (group_id,)
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_member_name(line_user_id: str) -> str:
    """ユーザーIDから表示名を取得する。

    Args:
        line_user_id: LINEユーザーID

    Returns:
        表示名（見つからない場合は「不明なユーザー」）
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT display_name FROM members WHERE line_user_id = ?",
            (line_user_id,)
        )
        row = cursor.fetchone()
        return row["display_name"] if row else "不明なユーザー"
    finally:
        conn.close()


# ─────────────────────────────────────
# タスク操作
# ─────────────────────────────────────

def add_task(
    content: str,
    assignee_id: str,
    due_date: date,
    group_id: str,
    created_by: str,
) -> int:
    """タスクを追加する。

    Args:
        content: やること
        assignee_id: 担当者のLINEユーザーID
        due_date: 期日
        group_id: グループID
        created_by: 作成者のLINEユーザーID

    Returns:
        追加されたタスクのID
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO tasks (content, assignee_id, due_date, group_id, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (content, assignee_id, due_date.isoformat(), group_id, created_by),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_pending_tasks(group_id: str) -> list[dict]:
    """グループの未完了タスク一覧を取得する。

    Args:
        group_id: グループID

    Returns:
        未完了タスクのリスト
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT t.id, t.content, t.assignee_id, t.due_date,
                   m.display_name AS assignee_name
            FROM tasks t
            LEFT JOIN members m ON t.assignee_id = m.line_user_id
            WHERE t.group_id = ? AND t.status = 'pending'
            ORDER BY t.due_date ASC
            """,
            (group_id,),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_task(task_id: int) -> Optional[dict]:
    """タスクIDからタスク情報を取得する。

    Args:
        task_id: タスクID

    Returns:
        タスク情報（見つからない場合はNone）
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT t.id, t.content, t.assignee_id, t.due_date, t.status, t.group_id,
                   m.display_name AS assignee_name
            FROM tasks t
            LEFT JOIN members m ON t.assignee_id = m.line_user_id
            WHERE t.id = ?
            """,
            (task_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_task_status(task_id: int, status: str) -> None:
    """タスクのステータスを更新する。

    Args:
        task_id: タスクID
        status: 新しいステータス（'pending' または 'completed'）
    """
    conn = get_connection()
    try:
        completed_at = datetime.now().isoformat() if status == "completed" else None
        conn.execute(
            "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
            (status, completed_at, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_task_due_date(task_id: int, due_date: date) -> None:
    """タスクの期日を変更する。

    Args:
        task_id: タスクID
        due_date: 新しい期日
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE tasks SET due_date = ? WHERE id = ?",
            (due_date.isoformat(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_task_assignee(task_id: int, assignee_id: str) -> None:
    """タスクの担当者を変更する。

    Args:
        task_id: タスクID
        assignee_id: 新しい担当者のLINEユーザーID
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE tasks SET assignee_id = ? WHERE id = ?",
            (assignee_id, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_tasks_due_today_or_tomorrow(today: date) -> list[dict]:
    """当日・翌日が期日の未完了タスクを取得する（リマインダー用）。

    Args:
        today: 今日の日付

    Returns:
        対象タスクのリスト
    """
    from datetime import timedelta
    tomorrow = today + timedelta(days=1)
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT t.id, t.content, t.assignee_id, t.due_date,
                   m.display_name AS assignee_name
            FROM tasks t
            LEFT JOIN members m ON t.assignee_id = m.line_user_id
            WHERE t.status = 'pending'
              AND t.due_date IN (?, ?)
            """,
            (today.isoformat(), tomorrow.isoformat()),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_overdue_tasks(today: date) -> list[dict]:
    """期限切れの未完了タスクを取得する（リマインダー用）。

    Args:
        today: 今日の日付

    Returns:
        期限切れタスクのリスト
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT t.id, t.content, t.assignee_id, t.due_date,
                   m.display_name AS assignee_name
            FROM tasks t
            LEFT JOIN members m ON t.assignee_id = m.line_user_id
            WHERE t.status = 'pending'
              AND t.due_date < ?
            """,
            (today.isoformat(),),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


# ─────────────────────────────────────
# 会話状態管理
# ─────────────────────────────────────

def get_conversation_state(user_id: str) -> Optional[dict]:
    """ユーザーの会話状態を取得する。

    Args:
        user_id: LINEユーザーID

    Returns:
        会話状態（state, temp_data を含む辞書）、存在しない場合はNone
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT state, temp_data FROM conversation_states WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "state": row["state"],
            "temp_data": json.loads(row["temp_data"]) if row["temp_data"] else {},
        }
    finally:
        conn.close()


def set_conversation_state(user_id: str, state: str, temp_data: dict = None) -> None:
    """ユーザーの会話状態を保存する。

    Args:
        user_id: LINEユーザーID
        state: 状態文字列
        temp_data: 一時保存データ（辞書形式）
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO conversation_states (user_id, state, temp_data, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                state = excluded.state,
                temp_data = excluded.temp_data,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, state, json.dumps(temp_data or {})),
        )
        conn.commit()
    finally:
        conn.close()


def clear_conversation_state(user_id: str) -> None:
    """ユーザーの会話状態を削除する。

    Args:
        user_id: LINEユーザーID
    """
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM conversation_states WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()
