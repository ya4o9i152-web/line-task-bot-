"""
main.py - LINEタスク管理Bot メインモジュール

FastAPIでWebhookを受信し、LINEのメッセージ・ポストバックイベントを処理する。
グループへのBot追加時にメンバーを自動登録し、会話形式でタスクを管理する。

使い方:
    uvicorn main:app --reload

環境変数（.envファイルに記述）:
    LINE_CHANNEL_SECRET      - LINE Botのチャネルシークレット
    LINE_CHANNEL_ACCESS_TOKEN - LINE Botのチャネルアクセストークン
"""

import os
from contextlib import asynccontextmanager
from datetime import date
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    FlexCarousel,
    FlexMessage,
    FlexBubble,
    FlexBox,
    FlexButton,
    FlexText,
    FlexSeparator,
    PostbackAction,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TextMessage,
    URIAction,
)
from linebot.v3.webhooks import (
    JoinEvent,
    MemberJoinedEvent,
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
)

import database as db
import scheduler as sched

# ─────────────────────────────────────
# 初期化
# ─────────────────────────────────────

load_dotenv()

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
parser = WebhookParser(LINE_CHANNEL_SECRET)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """アプリケーションの起動・終了時処理。"""
    # データベースの初期化
    db.init_db()

    # スケジューラーの起動
    scheduler = sched.create_scheduler(line_config)
    scheduler.start()

    yield

    # スケジューラーの停止
    scheduler.shutdown()


app = FastAPI(title="LINEタスク管理Bot", lifespan=lifespan)


# ─────────────────────────────────────
# Webhookエンドポイント
# ─────────────────────────────────────

@app.post("/callback")
async def callback(
    request: Request,
    x_line_signature: str = Header(alias="X-Line-Signature"),
) -> dict:
    """LINEからのWebhookを受信する。

    Args:
        request: FastAPIリクエストオブジェクト
        x_line_signature: LINE署名ヘッダー

    Returns:
        処理結果
    """
    body = await request.body()

    try:
        events = parser.parse(body.decode("utf-8"), x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="署名が無効です")

    async with AsyncApiClient(line_config) as api_client:
        line_api = AsyncMessagingApi(api_client)

        for event in events:
            if isinstance(event, JoinEvent):
                await handle_join(event, line_api)
            elif isinstance(event, MemberJoinedEvent):
                await handle_member_joined(event, line_api)
            elif isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
                await auto_register_member(event, line_api)
                await handle_text_message(event, line_api)
            elif isinstance(event, PostbackEvent):
                await auto_register_member(event, line_api)
                await handle_postback(event, line_api)

    return {"status": "ok"}


# ─────────────────────────────────────
# イベントハンドラー
# ─────────────────────────────────────

async def handle_join(event: JoinEvent, line_api: AsyncMessagingApi) -> None:
    """Botがグループに参加した時に既存メンバーを全員登録する。

    Args:
        event: Joinイベント
        line_api: LINE Messaging APIクライアント
    """
    group_id = event.source.group_id

    try:
        # グループの全メンバーIDを取得
        members_ids_response = await line_api.get_group_members_ids(group_id)
        for user_id in members_ids_response.member_ids:
            try:
                profile = await line_api.get_group_member_profile(group_id, user_id)
                db.add_member(user_id, profile.display_name, group_id)
            except Exception:
                pass
    except Exception:
        pass

    await line_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="✅ タスク管理Botが参加しました！\n「タスク追加」でタスクを追加できます📋")],
        )
    )


async def auto_register_member(event, line_api: AsyncMessagingApi) -> None:
    """メッセージ送信者がDB未登録の場合に自動登録する。

    Args:
        event: メッセージまたはポストバックイベント
        line_api: LINE Messaging APIクライアント
    """
    user_id = event.source.user_id
    group_id = getattr(event.source, "group_id", None)
    if group_id is None:
        return

    # 既に登録済みならスキップ
    if db.get_member_name(user_id) != "不明なユーザー":
        return

    try:
        profile = await line_api.get_group_member_profile(group_id, user_id)
        db.add_member(user_id, profile.display_name, group_id)
    except Exception:
        pass


async def handle_member_joined(event: MemberJoinedEvent, line_api: AsyncMessagingApi) -> None:
    """グループへの参加イベントを処理してメンバーを自動登録する。

    Args:
        event: メンバー参加イベント
        line_api: LINE Messaging APIクライアント
    """
    group_id = event.source.group_id

    for member in event.joined.members:
        user_id = member.user_id
        # プロフィールを取得して表示名を登録
        try:
            profile = await line_api.get_group_member_profile(group_id, user_id)
            display_name = profile.display_name
        except Exception:
            display_name = "メンバー"

        db.add_member(user_id, display_name, group_id)

    await line_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="✅ メンバーを登録しました！\n「タスク追加」でタスクを追加できます📋")],
        )
    )


async def handle_text_message(event: MessageEvent, line_api: AsyncMessagingApi) -> None:
    """テキストメッセージを処理する。

    会話状態に応じてタスク追加フロー・期日変更フローを進める。

    Args:
        event: メッセージイベント
        line_api: LINE Messaging APIクライアント
    """
    user_id = event.source.user_id
    text = event.message.text.strip()
    reply_token = event.reply_token

    # グループIDの取得（個人チャットの場合はuser_idを代用）
    group_id = getattr(event.source, "group_id", user_id)

    # 会話状態を確認
    conv = db.get_conversation_state(user_id)
    state = conv["state"] if conv else None
    temp_data = conv["temp_data"] if conv else {}

    # ─── コマンド処理 ───
    if text == "タスク追加":
        db.set_conversation_state(user_id, "adding_content", {"group_id": group_id})
        await _reply_text(line_api, reply_token, "📝 やることを入力してください")
        return

    if text == "タスク一覧":
        await send_task_list(line_api, reply_token, group_id)
        return

    if text == "キャンセル":
        db.clear_conversation_state(user_id)
        await _reply_text(line_api, reply_token, "❌ キャンセルしました")
        return

    # ─── 会話フロー処理 ───
    if state == "adding_content":
        # タスク内容を受け取り、担当者選択へ
        temp_data["content"] = text
        db.set_conversation_state(user_id, "adding_assignee", temp_data)
        members = db.get_members(temp_data["group_id"])
        if not members:
            db.clear_conversation_state(user_id)
            await _reply_text(line_api, reply_token, "⚠️ メンバーが登録されていません。グループにBotを追加してからお試しください。")
            return
        await _reply_text(
            line_api,
            reply_token,
            "👤 担当者を選んでください",
            quick_reply=_build_member_quick_reply(members, action="select_assignee"),
        )
        return

    if state == "adding_due_date":
        # 期日を受け取ってタスクを保存
        due_date = _parse_date(text)
        if due_date is None:
            await _reply_text(line_api, reply_token, "⚠️ 日付の形式が正しくありません。\n例：4/20 や 2026-04-20 のように入力してください")
            return

        task_id = db.add_task(
            content=temp_data["content"],
            assignee_id=temp_data["assignee_id"],
            due_date=due_date,
            group_id=temp_data["group_id"],
            created_by=user_id,
        )
        db.clear_conversation_state(user_id)

        assignee_name = db.get_member_name(temp_data["assignee_id"])
        await _reply_text(
            line_api,
            reply_token,
            f"✅ タスクを追加しました！\n\nやること：{temp_data['content']}\n担当：{assignee_name}\n期日：{_format_date(due_date.isoformat())}",
        )

        # 担当者に個人通知（自分以外の場合）
        if temp_data["assignee_id"] != user_id:
            creator_name = db.get_member_name(user_id)
            await line_api.push_message(
                PushMessageRequest(
                    to=temp_data["assignee_id"],
                    messages=[TextMessage(
                        text=f"📌 {creator_name}さんからタスクが割り当てられました\n\nやること：{temp_data['content']}\n期日：{_format_date(due_date.isoformat())}"
                    )],
                )
            )
        return

    if state and state.startswith("changing_due_date:"):
        # 期日変更の新しい日付を受け取る
        task_id = int(state.split(":")[1])
        due_date = _parse_date(text)
        if due_date is None:
            await _reply_text(line_api, reply_token, "⚠️ 日付の形式が正しくありません。\n例：4/20 や 2026-04-20 のように入力してください")
            return

        db.update_task_due_date(task_id, due_date)
        db.clear_conversation_state(user_id)
        await _reply_text(line_api, reply_token, f"📅 期日を変更しました → {_format_date(due_date.isoformat())}")
        return

    # ─── 未認識メッセージ ───
    if state:
        await _reply_text(line_api, reply_token, "「キャンセル」と送ると操作を中断できます")
    else:
        await _reply_text(
            line_api,
            reply_token,
            "使い方：\n・「タスク追加」でタスクを追加\n・「タスク一覧」でタスクを確認",
        )


async def handle_postback(event: PostbackEvent, line_api: AsyncMessagingApi) -> None:
    """ポストバックイベントを処理する（ボタン操作）。

    Args:
        event: ポストバックイベント
        line_api: LINE Messaging APIクライアント
    """
    user_id = event.source.user_id
    reply_token = event.reply_token
    group_id = getattr(event.source, "group_id", user_id)

    # クエリ文字列をパース
    params = {k: v[0] for k, v in parse_qs(event.postback.data).items()}
    action = params.get("action")
    task_id = int(params["task_id"]) if "task_id" in params else None

    if action == "complete":
        # タスク完了
        task = db.get_task(task_id)
        if task is None:
            await _reply_text(line_api, reply_token, "⚠️ タスクが見つかりませんでした")
            return

        db.update_task_status(task_id, "completed")
        completer_name = db.get_member_name(user_id)
        await _reply_text(
            line_api,
            reply_token,
            f"✅ {completer_name}さんが「{task['content']}」を完了しました！",
        )

    elif action == "change_due_date":
        # 期日変更：新しい期日の入力待ちへ
        db.set_conversation_state(user_id, f"changing_due_date:{task_id}")
        await _reply_text(line_api, reply_token, "📅 新しい期日を入力してください（例：4/25）")

    elif action == "change_assignee":
        # 担当変更：メンバー選択へ
        db.set_conversation_state(user_id, f"changing_assignee:{task_id}")
        members = db.get_members(group_id)
        if not members:
            db.clear_conversation_state(user_id)
            await _reply_text(line_api, reply_token, "⚠️ メンバーが登録されていません")
            return
        await _reply_text(
            line_api,
            reply_token,
            "👤 新しい担当者を選んでください",
            quick_reply=_build_member_quick_reply(members, action="select_new_assignee", task_id=task_id),
        )

    elif action == "select_assignee":
        # タスク追加時の担当者選択
        selected_user_id = params.get("user_id")
        conv = db.get_conversation_state(user_id)
        if conv is None or conv["state"] != "adding_assignee":
            await _reply_text(line_api, reply_token, "⚠️ 操作がタイムアウトしました。もう一度「タスク追加」から始めてください")
            return

        temp_data = conv["temp_data"]
        temp_data["assignee_id"] = selected_user_id
        db.set_conversation_state(user_id, "adding_due_date", temp_data)
        await _reply_text(line_api, reply_token, "📅 期日を入力してください（例：4/20）")

    elif action == "select_new_assignee":
        # 担当変更時の担当者選択
        new_assignee_id = params.get("user_id")
        conv = db.get_conversation_state(user_id)
        if conv is None or not conv["state"].startswith("changing_assignee:"):
            await _reply_text(line_api, reply_token, "⚠️ 操作がタイムアウトしました。もう一度やり直してください")
            return

        task_id = int(conv["state"].split(":")[1])
        task = db.get_task(task_id)
        if task is None:
            db.clear_conversation_state(user_id)
            await _reply_text(line_api, reply_token, "⚠️ タスクが見つかりませんでした")
            return

        db.update_task_assignee(task_id, new_assignee_id)
        db.clear_conversation_state(user_id)

        new_assignee_name = db.get_member_name(new_assignee_id)
        await _reply_text(line_api, reply_token, f"👤 担当者を変更しました → {new_assignee_name}さん")

        # 新しい担当者に個人通知（自分以外の場合）
        if new_assignee_id != user_id:
            changer_name = db.get_member_name(user_id)
            await line_api.push_message(
                PushMessageRequest(
                    to=new_assignee_id,
                    messages=[TextMessage(
                        text=f"📌 {changer_name}さんからタスクが割り当てられました\n\nやること：{task['content']}"
                    )],
                )
            )


# ─────────────────────────────────────
# タスク一覧表示
# ─────────────────────────────────────

async def send_task_list(
    line_api: AsyncMessagingApi,
    reply_token: str,
    group_id: str,
) -> None:
    """未完了タスクをFlex Messageで送信する。

    Args:
        line_api: LINE Messaging APIクライアント
        reply_token: リプライトークン
        group_id: グループID
    """
    tasks = db.get_pending_tasks(group_id)

    if not tasks:
        await _reply_text(line_api, reply_token, "📋 未完了のタスクはありません🎉")
        return

    # タスク件数が多い場合はテキスト形式で表示（Flex Messageは10件まで）
    if len(tasks) > 10:
        lines = [f"📋 未完了タスク一覧（{len(tasks)}件）\n"]
        for task in tasks:
            lines.append(
                f"・{task['content']}\n  担当：{task['assignee_name']} ／ 期日：{_format_date(task['due_date'])}"
            )
        lines.append("\n※ タスクを操作するには「タスク一覧」と送信してください（最新10件を表示）")
        await _reply_text(line_api, reply_token, "\n".join(lines))
        return

    # Flex Messageでボタン付きリストを表示
    bubbles = [_build_task_bubble(task) for task in tasks[:10]]

    # Flex Message（カルーセル形式）
    flex_message = FlexMessage(
        alt_text=f"📋 未完了タスク（{len(tasks)}件）",
        contents=FlexCarousel(contents=bubbles),
    )

    await line_api.reply_message(
        ReplyMessageRequest(
            reply_token=reply_token,
            messages=[flex_message],
        )
    )


def _build_task_bubble(task: dict) -> FlexBubble:
    """タスク1件分のFlex Bubbleを生成する。

    Args:
        task: タスク情報

    Returns:
        FlexBubbleオブジェクト
    """
    return FlexBubble(
        body=FlexBox(
            layout="vertical",
            contents=[
                FlexText(text=task["content"], weight="bold", size="md", wrap=True),
                FlexSeparator(margin="md"),
                FlexText(
                    text=f"👤 {task['assignee_name']}",
                    size="sm",
                    color="#555555",
                    margin="md",
                ),
                FlexText(
                    text=f"📅 {_format_date(task['due_date'])}",
                    size="sm",
                    color="#555555",
                    margin="sm",
                ),
            ],
        ),
        footer=FlexBox(
            layout="vertical",
            spacing="sm",
            contents=[
                FlexButton(
                    action=PostbackAction(
                        label="✅ 完了",
                        data=f"action=complete&task_id={task['id']}",
                    ),
                    style="primary",
                    color="#00C300",
                    height="sm",
                ),
                FlexButton(
                    action=PostbackAction(
                        label="📅 期日変更",
                        data=f"action=change_due_date&task_id={task['id']}",
                    ),
                    style="secondary",
                    height="sm",
                ),
                FlexButton(
                    action=PostbackAction(
                        label="👤 担当変更",
                        data=f"action=change_assignee&task_id={task['id']}",
                    ),
                    style="secondary",
                    height="sm",
                ),
            ],
        ),
    )


# ─────────────────────────────────────
# ユーティリティ関数
# ─────────────────────────────────────

async def _reply_text(
    line_api: AsyncMessagingApi,
    reply_token: str,
    text: str,
    quick_reply: QuickReply = None,
) -> None:
    """テキストメッセージを返信する。

    Args:
        line_api: LINE Messaging APIクライアント
        reply_token: リプライトークン
        text: 返信テキスト
        quick_reply: クイックリプライ（省略可）
    """
    message = TextMessage(text=text)
    if quick_reply:
        message.quick_reply = quick_reply

    await line_api.reply_message(
        ReplyMessageRequest(
            reply_token=reply_token,
            messages=[message],
        )
    )


def _build_member_quick_reply(
    members: list[dict],
    action: str,
    task_id: int = None,
) -> QuickReply:
    """メンバー選択用のクイックリプライを生成する。

    Args:
        members: メンバーリスト
        action: ポストバックアクション名
        task_id: タスクID（担当変更時のみ指定）

    Returns:
        QuickReplyオブジェクト
    """
    items = []
    for member in members:
        data = f"action={action}&user_id={member['line_user_id']}"
        if task_id is not None:
            data += f"&task_id={task_id}"

        items.append(
            QuickReplyItem(
                action=PostbackAction(
                    label=member["display_name"],
                    data=data,
                )
            )
        )

    return QuickReply(items=items)


def _parse_date(text: str) -> date | None:
    """テキストから日付をパースする。

    複数の形式に対応:
        - 4/20 → 当年の4月20日
        - 4-20 → 当年の4月20日
        - 2026-04-20 → 2026年4月20日

    Args:
        text: 入力テキスト

    Returns:
        dateオブジェクト（パース失敗時はNone）
    """
    text = text.strip()
    today = date.today()

    # M/D 形式
    for sep in ["/", "-", "."]:
        parts = text.split(sep)
        if len(parts) == 2:
            try:
                month, day = int(parts[0]), int(parts[1])
                parsed = date(today.year, month, day)
                # 過去日付の場合は翌年とする
                if parsed < today:
                    parsed = date(today.year + 1, month, day)
                return parsed
            except ValueError:
                continue

    # YYYY-M-D 形式
    parts = text.split("-")
    if len(parts) == 3:
        try:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            pass

    return None


def _format_date(date_str: str) -> str:
    """ISO形式の日付文字列を日本語表記に変換する。

    Args:
        date_str: ISO形式の日付文字列（例: '2026-04-20'）

    Returns:
        日本語表記の日付（例: '4月20日'）
    """
    d = date.fromisoformat(date_str)
    return f"{d.month}月{d.day}日"
