"""
scheduler.py - リマインダー・期限切れ通知モジュール

APSchedulerを使って毎朝8時に担当者へ通知を送信する。
"""

from datetime import date
from collections import defaultdict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    PushMessageRequest,
    TextMessage,
)

import database as db


def create_scheduler(line_config: Configuration) -> AsyncIOScheduler:
    """スケジューラーを作成して返す。

    Args:
        line_config: LINE Messaging APIの設定

    Returns:
        設定済みのAsyncIOSchedulerインスタンス
    """
    scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")

    # 毎朝8時にリマインダーを送信
    scheduler.add_job(
        send_reminders,
        trigger="cron",
        hour=8,
        minute=0,
        args=[line_config],
    )

    return scheduler


async def send_reminders(line_config: Configuration) -> None:
    """当日・翌日期日タスクと期限切れタスクを担当者に通知する。

    Args:
        line_config: LINE Messaging APIの設定
    """
    today = date.today()

    async with AsyncApiClient(line_config) as api_client:
        line_api = AsyncMessagingApi(api_client)

        await _send_due_reminders(line_api, today)
        await _send_overdue_reminders(line_api, today)


async def _send_due_reminders(line_api: AsyncMessagingApi, today: date) -> None:
    """当日・翌日期日のタスクを担当者に通知する。

    Args:
        line_api: LINE Messaging APIクライアント
        today: 今日の日付
    """
    tasks = db.get_tasks_due_today_or_tomorrow(today)
    if not tasks:
        return

    # 担当者ごとにタスクをまとめる
    tasks_by_assignee: dict[str, list[dict]] = defaultdict(list)
    for task in tasks:
        tasks_by_assignee[task["assignee_id"]].append(task)

    for assignee_id, assignee_tasks in tasks_by_assignee.items():
        today_tasks = [t for t in assignee_tasks if t["due_date"] == today.isoformat()]
        tomorrow_tasks = [t for t in assignee_tasks if t["due_date"] != today.isoformat()]

        lines = ["📋 本日のタスクリマインダーです\n"]

        if today_tasks:
            lines.append("【今日が期日】")
            for task in today_tasks:
                lines.append(f"⚠️ {task['content']}")

        if tomorrow_tasks:
            lines.append("\n【明日が期日】")
            for task in tomorrow_tasks:
                lines.append(f"📌 {task['content']}")

        message = "\n".join(lines)

        await line_api.push_message(
            PushMessageRequest(
                to=assignee_id,
                messages=[TextMessage(text=message)],
            )
        )


async def _send_overdue_reminders(line_api: AsyncMessagingApi, today: date) -> None:
    """期限切れタスクを担当者に通知する。

    Args:
        line_api: LINE Messaging APIクライアント
        today: 今日の日付
    """
    tasks = db.get_overdue_tasks(today)
    if not tasks:
        return

    # 担当者ごとにタスクをまとめる
    tasks_by_assignee: dict[str, list[dict]] = defaultdict(list)
    for task in tasks:
        tasks_by_assignee[task["assignee_id"]].append(task)

    for assignee_id, assignee_tasks in tasks_by_assignee.items():
        lines = ["🚨 期限切れのタスクがあります\n"]
        for task in assignee_tasks:
            lines.append(f"❌ {task['content']}（期日：{_format_date(task['due_date'])}）")

        message = "\n".join(lines)

        await line_api.push_message(
            PushMessageRequest(
                to=assignee_id,
                messages=[TextMessage(text=message)],
            )
        )


def _format_date(date_str: str) -> str:
    """ISO形式の日付文字列を日本語表記に変換する。

    Args:
        date_str: ISO形式の日付文字列（例: '2026-04-20'）

    Returns:
        日本語表記の日付（例: '4月20日'）
    """
    d = date.fromisoformat(date_str)
    return f"{d.month}月{d.day}日"
