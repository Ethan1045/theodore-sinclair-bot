"""对话历史管理：内存桶、摘要裁剪、Postgres 持久化。"""
import asyncio
import json as _json
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

import config
import state
import db as _db
from prompts import SYSTEM_PROMPT


def history_key_for(channel=None, message: "discord.Message | None" = None, interaction=None) -> str:
    ch = channel
    if ch is None and message is not None:
        ch = message.channel
    if ch is None and interaction is not None:
        ch = interaction.channel
    if ch is None:
        if interaction is not None and getattr(interaction, "channel_id", None):
            return f"ch:{interaction.channel_id}"
        return config.SYSTEM_HISTORY_KEY
    if isinstance(ch, discord.DMChannel):
        rec = getattr(ch, "recipient", None)
        rid = rec.id if rec else ch.id
        return f"dm:{rid}"
    return f"ch:{getattr(ch, 'id', config.SYSTEM_HISTORY_KEY)}"


def get_history(key: str = config.SYSTEM_HISTORY_KEY) -> list[dict]:
    h = state._histories.get(key)
    if h is None:
        h = [{"role": "system", "content": SYSTEM_PROMPT}]
        state._histories[key] = h
    state.touch_bucket(key)
    return h


def _msg_to_plain_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
    return str(content or "").strip()


async def _summarize_block(messages: list[dict]) -> str:
    from ai_client import ai_chat_create
    items = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        txt = _msg_to_plain_text(m)
        if not txt:
            continue
        items.append(f"{role}: {txt[:240]}")
    if len(items) < 2:
        return ""
    joined = "\n".join(items[-40:])
    prompt = (
        "下面是T.S.和用户的一段对话片段。请用一句中文（30-80字）客观总结：聊到了什么、"
        "有什么需要后续记住的事实/承诺/未完结话题。不要列项、不要换行、不要前缀。\n\n"
        f"{joined}\n\n摘要："
    )
    try:
        resp = await ai_chat_create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.3,
        )
        summary = (resp.choices[0].message.content or "").strip()
        summary = re.sub(r"<think>.*?</think>", "", summary, flags=re.DOTALL).strip()
        summary = summary.splitlines()[0].strip() if summary else ""
        return summary
    except Exception as e:
        print(f"⚠️ trim_history 摘要失败: {e}")
        return ""


async def trim_history(key: str = config.SYSTEM_HISTORY_KEY):
    """裁剪历史：必要时把中段用一句话摘要替换。

    本函数**自带分桶锁**：调用方在 append 完消息后，应在「释放 bucket_lock 之后」
    再调用 trim_history（asyncio.Lock 不可重入，持锁调用会死锁）。
    关键点：耗时的 LLM 摘要在锁外进行，不会长时间阻塞同一桶的其它消息。
    """
    state.mark_history_dirty(key)
    lock = state.get_bucket_lock(key)
    is_friend_dm = key.startswith("dm:") and key != f"dm:{config.PARTNER_USER_ID}"
    max_hist = config.FRIEND_MAX_HISTORY if is_friend_dm else config.MAX_HISTORY
    trim_to = max(3, max_hist - 3) if is_friend_dm else config.HISTORY_TRIM_TO
    # —— 阶段 1：持锁快照（快） ——
    async with lock:
        h = state._histories.get(key)
        if not h or len(h) <= max_hist:
            return
        if key in state._trimming:
            return
        state._trimming.add(key)
        middle = list(h[1:-trim_to])
        boundary = 1 + len(middle)  # 快照时刻 recent 段的起始下标
    try:
        # —— 阶段 2：锁外做 LLM 摘要（慢，最长可达 LLM 超时） ——
        summary_text = await _summarize_block(middle)
        # —— 阶段 3：持锁应用（快），boundary 之后＝原 recent 段 + 摘要期间新追加的消息 ——
        async with lock:
            h = state._histories.get(key)
            if not h or len(h) < boundary or h[0].get("role") != "system":
                # 桶在摘要期间被清理/重建/结构异常，放弃本次裁剪，保持现状
                return
            system_msg = h[0]
            recent = h[boundary:]
            if summary_text:
                digest = {
                    "role": "system",
                    "content": f"（早期对话摘要，覆盖约 {len(middle)} 条）{summary_text}",
                }
            else:
                digest = {
                    "role": "system",
                    "content": f"（早期对话已压缩，共裁剪约 {len(middle)} 条。请根据最近上下文推断。）",
                }
            h.clear()
            h.append(system_msg)
            h.append(digest)
            h.extend(recent)
    finally:
        state._trimming.discard(key)


# ==== Postgres 持久化 ====

async def ensure_conversation_history_table():
    if not config.DATABASE_URL:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS conversation_histories (
                        bucket_key TEXT PRIMARY KEY,
                        messages JSONB NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                await conn.commit()
        print("✅ conversation_histories 表已就绪")
    except Exception as e:
        print(f"⚠️ conversation_histories 表初始化失败: {e}")


def _serialize_history(hist: list[dict]) -> list[dict]:
    out: list[dict] = []
    for msg in hist[1:]:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, list):
            text = _msg_to_plain_text(msg)
            has_image = any(
                isinstance(p, dict) and p.get("type") == "image_url" for p in content
            )
            if has_image:
                text = (text + " （[图片]）").strip()
            content = text
        if not isinstance(content, str):
            content = str(content or "")
        saved = {"role": role, "content": content}
        if msg.get("source"):
            saved["source"] = str(msg["source"])
        out.append(saved)
    return out[-config.MAX_HISTORY:]


def _deserialize_history(saved: list) -> list[dict]:
    hist: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if isinstance(saved, list):
        for msg in saved:
            if isinstance(msg, dict) and msg.get("role") and msg.get("content") is not None:
                restored = {"role": msg["role"], "content": msg["content"]}
                if msg.get("source"):
                    restored["source"] = str(msg["source"])
                hist.append(restored)
    return hist


async def flush_dirty_histories():
    if not config.DATABASE_URL:
        state._dirty_history_buckets.clear()
        return
    keys = list(state._dirty_history_buckets)
    state._dirty_history_buckets.clear()
    if not keys:
        return
    payloads: list[tuple[str, str]] = []
    for key in keys:
        hist = state._histories.get(key)
        if not hist:
            continue
        serialized = _serialize_history(hist)
        if not serialized:
            continue
        payloads.append((key, _json.dumps(serialized, ensure_ascii=False)))
    if not payloads:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                for key, blob in payloads:
                    await cur.execute(
                        """INSERT INTO conversation_histories (bucket_key, messages, updated_at)
                           VALUES (%s, %s::jsonb, NOW())
                           ON CONFLICT (bucket_key)
                           DO UPDATE SET messages = EXCLUDED.messages, updated_at = NOW()""",
                        (key, blob),
                    )
            await conn.commit()
    except Exception as e:
        for key, _ in payloads:
            state._dirty_history_buckets.add(key)
        print(f"⚠️ 持久化对话历史失败（{len(payloads)} 个桶）：{e}")


async def delete_persisted_history(key: str) -> None:
    state._dirty_history_buckets.discard(key)
    if not config.DATABASE_URL:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM conversation_histories WHERE bucket_key = %s", (key,)
                )
            await conn.commit()
    except Exception as e:
        print(f"⚠️ 删除对话历史 {key} 失败：{e}")


async def load_all_histories():
    if not config.DATABASE_URL:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT bucket_key, messages, updated_at FROM conversation_histories"
                    " WHERE bucket_key LIKE 'dm:%%' OR bucket_key LIKE 'ch:%%'"
                )
                rows = await cur.fetchall()
    except Exception as e:
        print(f"⚠️ 读取对话历史失败: {e}")
        return
    restored = 0
    for bucket_key, messages, updated_at in rows:
        try:
            saved = messages if isinstance(messages, list) else _json.loads(messages)
            hist = _deserialize_history(saved)
            if len(hist) <= 1:
                continue
            # 旧版主动私信与睡醒补回只写 assistant、不写 user，久而久之会把
            # DM 桶灌成一串 Morning。这样的单边记录不是对话，不恢复进模型上下文。
            if not any(msg.get("role") == "user" for msg in hist[1:]):
                continue
            state._histories[bucket_key] = hist
            if updated_at is not None:
                state._bucket_touched[bucket_key] = updated_at
            restored += 1
        except Exception as e:
            print(f"⚠️ 恢复桶 {bucket_key} 失败：{e}")
    if restored:
        print(f"✅ 已从数据库恢复 {restored} 个对话桶")


async def remember_outbound_context(key: str, content: str, *, source: str) -> None:
    """保留最近一条主动消息，供恋人接话；不让单边自动消息无限堆满桶。"""
    text = str(content or "").strip()
    if not text:
        return
    lock = state.get_bucket_lock(key)
    async with lock:
        hist = get_history(key)
        while len(hist) > 1 and hist[-1].get("source") == source:
            hist.pop()
        if not any(msg.get("role") == "user" for msg in hist[1:]):
            hist[1:] = []
        hist.append({"role": "assistant", "content": text, "source": source})
        state.mark_history_dirty(key)
    await trim_history(key)


async def ensure_conversation_events_table() -> None:
    """按时间保存真实聊天轮次，供每日摘要准确读取“昨天”的内容。"""
    if not config.DATABASE_URL:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS theodore_conversation_events (
                        id BIGSERIAL PRIMARY KEY,
                        bucket_key TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'chat',
                        occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_theodore_conversation_events_time
                    ON theodore_conversation_events (occurred_at)
                """)
                await conn.commit()
        print("✅ theodore_conversation_events 表已就绪")
    except Exception as exc:
        print(f"⚠️ theodore_conversation_events 表初始化失败: {exc}")


async def record_conversation_event(
    bucket_key: str,
    role: str,
    content: str,
    *,
    source: str = "chat",
) -> None:
    if not config.DATABASE_URL or role not in {"user", "assistant"}:
        return
    clean = re.sub(r"\s+", " ", str(content or "")).strip()[:4000]
    if not clean:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO theodore_conversation_events
                           (bucket_key, role, content, source)
                       VALUES (%s, %s, %s, %s)""",
                    (bucket_key, role, clean, source),
                )
                await conn.commit()
    except Exception as exc:
        print(f"⚠️ 记录每日摘要素材失败: {exc}")


async def fetch_conversation_events_for_day(target_date: date) -> list[tuple[str, str, str]]:
    if not config.DATABASE_URL:
        return []
    beijing = ZoneInfo("Asia/Shanghai")
    start = datetime.combine(target_date, time.min, tzinfo=beijing).astimezone(timezone.utc)
    end = (datetime.combine(target_date, time.min, tzinfo=beijing) + timedelta(days=1)).astimezone(timezone.utc)
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT role, content, bucket_key
                       FROM theodore_conversation_events
                       WHERE occurred_at >= %s AND occurred_at < %s AND source='chat'
                       ORDER BY occurred_at ASC, id ASC""",
                    (start, end),
                )
                return await cur.fetchall()
    except Exception as exc:
        print(f"⚠️ 读取每日摘要素材失败: {exc}")
        return []


async def prune_conversation_events(*, keep_days: int = 45) -> None:
    if not config.DATABASE_URL:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM theodore_conversation_events WHERE occurred_at < NOW() - (%s * INTERVAL '1 day')",
                    (max(7, keep_days),),
                )
                await conn.commit()
    except Exception as exc:
        print(f"⚠️ 清理每日摘要素材失败: {exc}")
