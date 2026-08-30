"""Persistent London-day schedule used by presence, replies and proactive tasks."""
from __future__ import annotations

import json
import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

import config
import state
import db as _db
from client import discord_client
from presence import generate_custom_bubble

LONDON = ZoneInfo("Europe/London")


async def ensure_life_schedule_table() -> None:
    if not config.DATABASE_URL:
        return
    try:
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS daily_life_schedules (
                        schedule_date DATE PRIMARY KEY,
                        schedule_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                await conn.commit()
        print("✅ daily_life_schedules 表已就绪")
    except Exception as exc:
        print(f"⚠️ daily_life_schedules 表初始化失败: {exc}")


def _slot(start: str, end: str, key: str, label: str, kind: str, presence: str,
          availability: str = "available", proactive: str = "") -> dict:
    return {
        "start": start, "end": end, "key": key, "label": label,
        "kind": kind, "presence": presence, "availability": availability,
        "proactive": proactive,
    }


def _build_schedule(now: datetime) -> list[dict]:
    """Generate one coherent day. Random choices are date-seeded and therefore stable."""
    rng = random.Random(f"life:{now.date().isoformat()}:{config.PARTNER_USER_ID}")
    music = rng.choice([
        "Bill Evans — Peace Piece", "Chet Baker — Almost Blue",
        "Bach — Goldberg Variations", "Ryuichi Sakamoto — async",
    ])
    evening = rng.choice([
        _slot("19:30", "21:00", "fencing", "在击剑馆训练", "playing", "Evening fencing", "limited", "训练后的安静片刻"),
        _slot("19:30", "21:00", "swim", "在泳池训练", "playing", "Late swim", "limited", "游完泳之后"),
        _slot("19:30", "21:00", "reading", "在书房阅读", "playing", "Reading in the study", "available", "刚读到的一页"),
    ])
    if now.weekday() < 5:
        slots = [
            _slot("00:00", "05:30", "sleep", "在睡觉", "custom", "sleeping", "asleep"),
            _slot("05:30", "07:30", "morning", "在家醒来、喝茶并看晨间文件", "custom", "", "available", "晨间天气或茶"),
            _slot("07:30", "09:00", "commute", "去办公室的路上", "listening", music, "limited"),
            _slot("09:00", "12:30", "office_am", "在家族办公室处理文件与会议", "playing", "Family-office papers", "limited", "上午会议后的一个念头"),
            _slot("12:30", "14:00", "lunch", "午餐并短暂离开办公桌", "custom", "", "available", "午餐间隙"),
            _slot("14:00", "18:00", "foundation", "处理基金会与文保项目", "playing", "Foundation & archive work", "limited", "档案或修复工作"),
            _slot("18:00", "19:30", "return", "回家并整理当天的事", "listening", music, "limited"),
            evening,
            _slot("21:00", "23:15", "study", "在书房，已经结束正式工作", "custom", "", "available", "书房里的小事"),
            _slot("23:15", "23:59", "wind_down", "准备休息", "custom", "winding down…", "limited"),
        ]
    else:
        slots = [
            _slot("00:00", "06:30", "sleep", "在睡觉", "custom", "sleeping", "asleep"),
            _slot("06:30", "09:30", "slow_morning", "在家过一个安静的早晨", "custom", "", "available", "周末早晨"),
            _slot("09:30", "12:30", "estate", "处理私人信件和家中事务", "playing", "Letters & household papers", "limited"),
            _slot("12:30", "15:00", "lunch_walk", "午餐后在伦敦散步", "watching", "London, unhurried", "available", "沿路看到的事"),
            _slot("15:00", "18:30", "archive", "在档案室或书房阅读", "playing", "Archive afternoon", "limited", "旧纸与书"),
            evening,
            _slot("21:00", "23:30", "study", "在书房听音乐", "listening", music, "available", "正在听的音乐"),
            _slot("23:30", "23:59", "wind_down", "准备休息", "custom", "winding down…", "limited"),
        ]
    return slots


async def _load_or_create_schedule(now: datetime) -> list[dict]:
    day = now.date()
    if state.daily_life_date == day and state.daily_life_schedule:
        return state.daily_life_schedule

    schedule = None
    if config.DATABASE_URL:
        try:
            async with _db.db_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT schedule_json FROM daily_life_schedules WHERE schedule_date=%s",
                        (day,),
                    )
                    row = await cur.fetchone()
                    if row:
                        schedule = row[0]
                        if isinstance(schedule, str):
                            schedule = json.loads(schedule)
                    else:
                        schedule = _build_schedule(now)
                        await cur.execute(
                            "INSERT INTO daily_life_schedules(schedule_date, schedule_json) VALUES(%s, %s::jsonb) "
                            "ON CONFLICT(schedule_date) DO NOTHING",
                            (day, json.dumps(schedule, ensure_ascii=False)),
                        )
                        await conn.commit()
        except Exception as exc:
            print(f"⚠️ 每日日程读写失败，使用进程内日程: {exc}")
    if not schedule:
        schedule = _build_schedule(now)
    state.daily_life_date = day
    state.daily_life_schedule = schedule
    return schedule


def _minutes(hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m


def _current_slot(schedule: list[dict], now: datetime) -> dict:
    minute = now.hour * 60 + now.minute
    for item in schedule:
        start, end = _minutes(item["start"]), _minutes(item["end"])
        if start <= minute <= end:
            return item
    return schedule[-1]


def slot_end_utc(slot: dict, now_london: datetime) -> datetime:
    h, m = (int(x) for x in slot["end"].split(":"))
    end = datetime.combine(now_london.date(), time(h, m), tzinfo=LONDON)
    if end <= now_london:
        end += timedelta(days=1)
    return end.astimezone(timezone.utc)


async def refresh_life_state(*, force_presence: bool = False) -> tuple[dict, bool]:
    now = datetime.now(LONDON)
    schedule = await _load_or_create_schedule(now)
    slot = _current_slot(schedule, now)
    old_key = (state.current_life_slot or {}).get("key")
    changed = old_key != slot.get("key")
    state.current_life_slot = dict(slot)

    if slot.get("availability") == "busy":
        state.work_busy_activity = slot["label"]
        state.work_busy_until = slot_end_utc(slot, now)

    if changed or force_presence:
        text = slot.get("presence", "")
        kind = slot.get("kind", "playing")
        if kind == "custom" and not text:
            # The existing generator is now tied to selected day-plan transitions.
            text = await generate_custom_bubble()
            slot["presence"] = text
            state.current_life_slot["presence"] = text
            if config.DATABASE_URL:
                try:
                    async with _db.db_conn() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "UPDATE daily_life_schedules SET schedule_json=%s::jsonb, updated_at=NOW() "
                                "WHERE schedule_date=%s",
                                (json.dumps(schedule, ensure_ascii=False), now.date()),
                            )
                            await conn.commit()
                except Exception as exc:
                    print(f"⚠️ 气泡状态持久化失败: {exc}")
        if kind == "custom":
            activity = discord.CustomActivity(name=text)
        else:
            activity = discord.Activity(
                type={
                    "playing": discord.ActivityType.playing,
                    "listening": discord.ActivityType.listening,
                    "watching": discord.ActivityType.watching,
                }.get(kind, discord.ActivityType.playing),
                name=text,
            )
        status = discord.Status.idle if slot.get("availability") in {"busy", "limited", "asleep"} else discord.Status.online
        try:
            await discord_client.change_presence(status=status, activity=activity)
            state.set_current_presence(kind, text, source=f"life:{slot['key']}", duration_type="sustained")
            print(f"🗓️ 日程状态 → {slot['label']} [{kind}] {text}")
        except Exception as exc:
            print(f"⚠️ 日程 Presence 更新失败: {exc}")
    return state.current_life_slot, changed
