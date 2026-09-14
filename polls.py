"""Persistence and end-of-poll follow-up for native Discord polls."""
from datetime import datetime, timezone

import discord

import config
import db as _db
import state
from ai_client import call_ai
from client import discord_client
from directives import parse_bot_directives
from prompts import SYSTEM_PROMPT


async def ensure_poll_followup_table() -> None:
    if not config.DATABASE_URL:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS poll_followups (
                        message_id BIGINT PRIMARY KEY,
                        guild_id BIGINT NOT NULL,
                        channel_id BIGINT NOT NULL,
                        question TEXT NOT NULL,
                        ends_at TIMESTAMPTZ NOT NULL,
                        followed_up_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                await cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_poll_followups_due ON poll_followups(ends_at, followed_up_at)"
                )
                await cur.execute(
                    "ALTER TABLE poll_followups ADD COLUMN IF NOT EXISTS fail_count INT NOT NULL DEFAULT 0"
                )
                await conn.commit()
        print("✅ poll_followups 表已就绪")
    except Exception as exc:
        print(f"⚠️ poll_followups 表初始化失败: {exc}")


async def record_poll(message, question: str, ends_at: datetime) -> None:
    if not config.DATABASE_URL or not getattr(message, "guild", None):
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO poll_followups(message_id, guild_id, channel_id, question, ends_at)
                    VALUES(%s, %s, %s, %s, %s)
                    ON CONFLICT(message_id) DO NOTHING
                """, (message.id, message.guild.id, message.channel.id, question[:300], ends_at))
                await conn.commit()
    except Exception as exc:
        print(f"⚠️ 记录投票失败: {exc}")


async def check_ended_polls() -> None:
    if not config.DATABASE_URL:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE poll_followups AS claimed SET followed_up_at=NOW()
                    WHERE claimed.message_id IN (
                        SELECT candidate.message_id FROM poll_followups candidate
                        WHERE candidate.followed_up_at IS NULL AND candidate.ends_at <= NOW()
                          AND candidate.fail_count < 5
                        ORDER BY candidate.ends_at FOR UPDATE SKIP LOCKED LIMIT 3
                    )
                    RETURNING claimed.message_id, claimed.channel_id, claimed.question
                """)
                rows = await cur.fetchall()
                await conn.commit()
        for message_id, channel_id, question in rows:
            try:
                channel = await discord_client.fetch_channel(channel_id)
                message = await channel.fetch_message(message_id)
                poll = getattr(message, "poll", None)
                if poll is None:
                    result_text = "结果不可用"
                else:
                    answers = []
                    for answer in poll.answers:
                        answers.append(f"{answer.text}: {int(answer.vote_count or 0)}票")
                    result_text = "；".join(answers) or "没有人投票"
                prompt = (
                    f"（系统提示：你发起的Discord投票「{question}」结束了，结果是：{result_text}。"
                    "请用一句克制自然的话评论结果。严格两行：英文一行、括号中文一行；"
                    "不输出REACTION/ACTION/SPLIT。）" + state.life_hint_text()
                )
                raw = await call_ai([
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ])
                _, messages, _, _, _ = parse_bot_directives(raw)
                await message.reply(messages[0] if messages else f"The result is in.\n（结果出来了：{result_text}。）")
            except discord.NotFound:
                print(f"ℹ️ 投票消息已删除，停止回访: {message_id}")
            except Exception as exc:
                print(f"⚠️ 投票结束回访失败 message={message_id}: {exc}")
                try:
                    async with _db.db_conn() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "UPDATE poll_followups SET followed_up_at=NULL, fail_count=fail_count+1 "
                                "WHERE message_id=%s",
                                (message_id,),
                            )
                            await conn.commit()
                except Exception as reset_exc:
                    print(f"⚠️ 重置投票领取状态失败: {reset_exc}")
    except Exception as exc:
        print(f"⚠️ 查询已结束投票失败: {exc}")
