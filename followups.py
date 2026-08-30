"""恋人-only unfinished-plan ledger and conservative follow-up delivery."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import db as _db
import state
from ai_client import call_ai
from client import discord_client
from directives import parse_bot_directives
from prompts import SYSTEM_PROMPT

BEIJING = ZoneInfo("Asia/Shanghai")
_PLAN_VERBS = re.compile(
    r"(?:我要|我得|我准备|我打算|我计划|我等|准备去|打算去|要去|得去|"
    r"要交|要写|要做|要考|要面试|等外卖|等快递|等结果|等通知)", re.I,
)
_TIME_WORDS = re.compile(
    r"(?:今天|今晚|今早|明天|明早|明晚|后天|待会|一会儿|下午|上午|晚上|"
    r"\d+(?:\.\d+)?\s*(?:分钟|小时|天)后|周[一二三四五六日天]|下周)", re.I,
)
_TIMED_ACTION = re.compile(r"(?:交|去|做|写|考|面试|等|拿|取|看|复诊|开会|出门|回来)", re.I)
_DONE_WORDS = re.compile(
    r"(?:弄完了|做完了|写完了|交了|结束了|完成了|考完了|面完了|到了|收到了|"
    r"取到了|回来了|看完了|已经办好|搞定|done|finished|completed)", re.I,
)
_STOP_WORDS = {"我", "今天", "明天", "后天", "下午", "上午", "晚上", "今晚", "已经", "准备", "打算", "计划", "一个", "这个"}


async def ensure_followup_table() -> None:
    if not config.DATABASE_URL:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS commitment_followups (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        source_message_id BIGINT UNIQUE,
                        source_channel_id BIGINT,
                        item TEXT NOT NULL,
                        due_at TIMESTAMPTZ NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        followup_at TIMESTAMPTZ,
                        fail_count INT NOT NULL DEFAULT 0,
                        resolved_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                await cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_commitment_due "
                    "ON commitment_followups(user_id, status, due_at)"
                )
                await cur.execute(
                    "ALTER TABLE commitment_followups "
                    "ADD COLUMN IF NOT EXISTS fail_count INT NOT NULL DEFAULT 0"
                )
                await conn.commit()
        print("✅ commitment_followups 表已就绪")
    except Exception as exc:
        print(f"⚠️ commitment_followups 表初始化失败: {exc}")


def _due_from_text(text: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(BEIJING)
    rel = re.search(r"(\d+(?:\.\d+)?)\s*(分钟|小时|天)后", text)
    if rel:
        value = float(rel.group(1))
        unit = rel.group(2)
        delta = timedelta(minutes=value) if unit == "分钟" else timedelta(hours=value) if unit == "小时" else timedelta(days=value)
        return (now + delta).astimezone(timezone.utc)

    day_offset = 2 if "后天" in text else 1 if "明天" in text or "明早" in text or "明晚" in text else 0
    target_day = (now + timedelta(days=day_offset)).date()
    if "明早" in text or "上午" in text or "今早" in text:
        hour = 11
    elif "下午" in text:
        hour = 18
    elif "晚上" in text or "今晚" in text or "明晚" in text:
        hour = 22
    elif "待会" in text or "一会儿" in text or any(k in text for k in ("等外卖", "等快递")):
        return (now + timedelta(hours=2)).astimezone(timezone.utc)
    else:
        hour = 19
    due = datetime.combine(target_day, datetime.min.time(), tzinfo=BEIJING).replace(hour=hour)
    if due <= now:
        due = now + timedelta(hours=3)
    return due.astimezone(timezone.utc)


def _looks_like_plan(text: str) -> bool:
    text = (text or "").strip()
    if not (4 <= len(text) <= 500):
        return False
    if text.endswith(("?", "？")) or "要不要" in text:
        return False
    has_time = bool(_TIME_WORDS.search(text))
    return bool(_PLAN_VERBS.search(text) and (has_time or "等" in text)) or bool(has_time and _TIMED_ACTION.search(text))


def _keywords(text: str) -> set[str]:
    chunks = set(re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z]{3,}", text.lower()))
    return {c for c in chunks if c not in _STOP_WORDS}


async def _close_matching(text: str) -> bool:
    if not config.DATABASE_URL or not _DONE_WORDS.search(text or ""):
        return False
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE commitment_followups SET status='expired' "
                    "WHERE status='open' AND due_at < NOW() - INTERVAL '7 days'"
                )
                await cur.execute("""
                    SELECT id, item FROM commitment_followups
                    WHERE user_id=%s AND status='open'
                    ORDER BY due_at DESC LIMIT 8
                """, (config.PARTNER_USER_ID,))
                rows = await cur.fetchall()
                if not rows:
                    return False
                incoming = _keywords(text)
                chosen = None
                for row_id, item in rows:
                    if incoming & _keywords(item):
                        chosen = row_id
                        break
                # A bare “弄完了” naturally resolves only the most recent item.
                if chosen is None and len(rows) == 1:
                    chosen = rows[0][0]
                if chosen is None:
                    return False
                await cur.execute(
                    "UPDATE commitment_followups SET status='done', resolved_at=NOW() WHERE id=%s",
                    (chosen,),
                )
                await conn.commit()
        print(f"✅ 未完事项已关闭: #{chosen}")
        return True
    except Exception as exc:
        print(f"⚠️ 关闭未完事项失败: {exc}")
        return False


async def capture_partner_message(message, text: str) -> None:
    """Observe 恋人 text. Never creates entries for vague wishes without a time cue."""
    if getattr(message.author, "id", None) != config.PARTNER_USER_ID or not config.DATABASE_URL:
        return
    if await _close_matching(text):
        return
    if not _looks_like_plan(text):
        return
    due = _due_from_text(text)
    if due is None:
        return
    item = re.sub(r"\s+", " ", text).strip()[:500]
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                # Avoid near-duplicate promises within two days as well as duplicate events.
                await cur.execute("""
                    SELECT item FROM commitment_followups
                    WHERE user_id=%s AND status='open' AND created_at > NOW() - INTERVAL '2 days'
                """, (config.PARTNER_USER_ID,))
                existing = await cur.fetchall()
                if any(_keywords(item) and len(_keywords(item) & _keywords(row[0])) >= 1 for row in existing):
                    return
                await cur.execute("""
                    INSERT INTO commitment_followups
                        (user_id, source_message_id, source_channel_id, item, due_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT(source_message_id) DO NOTHING
                """, (
                    config.PARTNER_USER_ID, getattr(message, "id", None),
                    getattr(getattr(message, "channel", None), "id", None), item, due,
                ))
                await conn.commit()
        print(f"📝 未完事项记下: {item[:80]}（{due.isoformat()}）")
    except Exception as exc:
        print(f"⚠️ 保存未完事项失败: {exc}")


async def add_manual_followup(message, *, hours: int = 24) -> bool:
    if not config.DATABASE_URL:
        return False
    text = (getattr(message, "content", "") or "（无文字消息）").strip()[:500]
    due = datetime.now(timezone.utc) + timedelta(hours=max(1, min(hours, 168)))
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO commitment_followups
                        (user_id, source_message_id, source_channel_id, item, due_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT(source_message_id) DO UPDATE SET
                        item=EXCLUDED.item, due_at=EXCLUDED.due_at, status='open',
                        followup_at=NULL, fail_count=0, resolved_at=NULL
                """, (config.PARTNER_USER_ID, message.id, message.channel.id, text, due))
                await conn.commit()
        return True
    except Exception as exc:
        print(f"⚠️ 手动添加未完事项失败: {exc}")
        return False


async def check_due_followups() -> None:
    """Send at most one follow-up per 8h, during daytime, and never repeat one item."""
    if not config.DATABASE_URL:
        return
    now_bj = datetime.now(BEIJING)
    if not 9 <= now_bj.hour < 23:
        return
    if state.last_partner_activity_at:
        idle = (datetime.now(timezone.utc) - state.last_partner_activity_at).total_seconds()
        if idle < 20 * 60:
            return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE commitment_followups SET status='expired' "
                    "WHERE status='open' AND due_at < NOW() - INTERVAL '7 days'"
                )
                # Serialize the global 8-hour anti-spam decision across replicas.
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"followup:{config.PARTNER_USER_ID}",),
                )
                await cur.execute("""
                    UPDATE commitment_followups AS claimed
                    SET followup_at=NOW()
                    WHERE claimed.id = (
                        SELECT candidate.id FROM commitment_followups AS candidate
                        WHERE candidate.user_id=%s AND candidate.status='open'
                          AND candidate.followup_at IS NULL
                          AND candidate.fail_count < 5
                          AND candidate.due_at < NOW() - INTERVAL '45 minutes'
                          AND candidate.due_at > NOW() - INTERVAL '7 days'
                          AND NOT EXISTS (
                              SELECT 1 FROM commitment_followups recent
                              WHERE recent.user_id=%s
                                AND recent.followup_at > NOW() - INTERVAL '8 hours'
                          )
                        ORDER BY candidate.due_at ASC
                        FOR UPDATE SKIP LOCKED LIMIT 1
                    )
                    RETURNING claimed.id, claimed.item
                """, (config.PARTNER_USER_ID, config.PARTNER_USER_ID))
                row = await cur.fetchone()
                await conn.commit()
        if not row:
            return
        row_id, item = row
        prompt = (
            f"（系统提示：恋人之前说过「{item}」。预计时间已经过去了。"
            "请像恋人自然接着问一句结果，不要说提醒、记录、事项、系统，也不要施压。"
            "严格两行：第一行英文，第二行括号中文；不要REACTION/ACTION/SPLIT。）"
            + state.life_hint_text()
        )
        raw = await call_ai([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ])
        _, messages, _, _, _ = parse_bot_directives(raw)
        if not messages:
            raise ValueError("AI 未生成可发送的追问")
        partner = await discord_client.fetch_user(config.PARTNER_USER_ID)
        await partner.send(messages[0])
        print(f"✅ 已自然追问未完事项 #{row_id}")
    except Exception as exc:
        print(f"⚠️ 未完事项追问失败（保留待重试）: {exc}")
        if "row_id" in locals():
            try:
                async with _db.db_conn() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE commitment_followups "
                            "SET followup_at=NULL, fail_count=fail_count+1, "
                            "status=CASE WHEN fail_count+1>=5 THEN 'expired' ELSE status END "
                            "WHERE id=%s AND status='open'",
                            (row_id,),
                        )
                        await conn.commit()
            except Exception as reset_exc:
                print(f"⚠️ 重置未完事项领取状态失败: {reset_exc}")
