"""Persistent daily schedule used by presence, replies and proactive tasks.

平时是伦敦的一天；出差期间整张日程换成出差版本，时段按目的地当地时间排，
状态栏也带上那座城市。出发/返程会让当天剩下的日程作废并重建。
"""
from __future__ import annotations

import json
import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

import config
import presence
import state
import trips
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
          availability: str = "available", proactive: str = "",
          anchor: str = "", anchor_kind: str = "playing") -> dict:
    """一个时段。

    presence 为空的 custom 时段＝「这里挂一句 AI 现编的碎碎念」，气泡只在时段开头
    挂 presence.TRANSIENT_MINUTES 分钟，之后落回 anchor 这个稳定的活动；
    其余时段 anchor 就等于 presence，不涉及任何 AI 调用。
    """
    bubble = kind == "custom" and not presence
    return {
        "start": start, "end": end, "key": key, "label": label,
        "kind": kind, "presence": presence, "availability": availability,
        "proactive": proactive,
        "bubble": bubble,
        "anchor": anchor or presence,
        "anchor_kind": anchor_kind if bubble else kind,
    }


def _build_trip_schedule(rng: random.Random, destination: dict) -> list[dict]:
    """出差的一天：按目的地当地时间排，白天在会议/工坊里，夜里在酒店。

    比在家那套更「不便长聊」，但晚上留出完整的可聊时段——人在外地不等于消失。
    """
    day_text, day_label = destination["day"]
    night_text, night_label = destination["night"]
    city_cn, city_en = destination["city_cn"], destination["city_en"]
    extra_kind, extra_text, extra_label = rng.choice(trips.TRIP_ANCHOR_EXTRA)
    dinner = rng.choice([
        _slot("18:00", "21:00", "trip_dinner", "和当地的人吃晚饭", "watching",
              f"{city_en}, evening", "limited", "饭桌上听来的一句话"),
        _slot("18:00", "21:00", "trip_dinner", f"推掉了应酬，自己在{city_cn}走了走", "watching",
              f"{city_en}, evening", "available", f"{city_cn}街上看到的东西"),
    ])
    return [
        _slot("00:00", "07:00", "sleep", f"在{city_cn}的酒店里睡觉", "custom", "sleeping", "asleep"),
        _slot("07:00", "09:00", "trip_morning", f"{city_cn}的清晨，倒时差、看邮件", "custom", "",
              "available", "时差和窗外的天色", anchor=f"{city_en}, morning"),
        # 用 limited 而不是 busy：在家的工作时段也是 limited，人在外地不该比在家更难找到他。
        _slot("09:00", "12:30", "trip_day", day_label, "playing", day_text, "limited"),
        _slot("12:30", "13:30", "trip_lunch", "会议间隙，一个人吃午饭", extra_kind, extra_text,
              "available", "两场会之间的空档"),
        _slot("13:30", "16:00", "trip_day_pm", day_label, "playing", day_text, "limited"),
        _slot("16:00", "18:00", "trip_between", extra_label, extra_kind, extra_text, "limited"),
        dinner,
        # 夜里这段留成完整的可聊时段：和在家的日程一样，晚上是他真正有空的时候。
        _slot("21:00", "23:30", "trip_night", night_label, "playing", night_text,
              "available", f"{city_cn}的夜里"),
        _slot("23:30", "23:59", "trip_wind_down", f"在{city_cn}写日记、准备休息", "custom",
              "winding down…", "limited"),
    ]


def _build_schedule(now: datetime, trip_code: str = "") -> list[dict]:
    """Generate one coherent day. Random choices are date-seeded and therefore stable."""
    rng = random.Random(f"life:{now.date().isoformat()}:{trip_code}:{config.PARTNER_USER_ID}")
    destination = trips.destination_by_code(trip_code)
    if destination:
        return _build_trip_schedule(rng, destination)
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
            _slot("05:30", "07:30", "morning", "在家醒来、喝茶并看晨间文件", "custom", "", "available",
                  "晨间天气或茶", anchor="Morning papers"),
            _slot("07:30", "09:00", "commute", "去办公室的路上", "listening", music, "limited"),
            _slot("09:00", "12:30", "office_am", "在家族办公室处理文件与会议", "playing", "Family-office papers", "limited", "上午会议后的一个念头"),
            _slot("12:30", "14:00", "lunch", "午餐并短暂离开办公桌", "custom", "", "available",
                  "午餐间隙", anchor="Away from the desk"),
            _slot("14:00", "18:00", "foundation", "处理基金会与文保项目", "playing", "Foundation & archive work", "limited", "档案或修复工作"),
            _slot("18:00", "19:30", "return", "回家并整理当天的事", "listening", music, "limited"),
            evening,
            _slot("21:00", "23:15", "study", "在书房，已经结束正式工作", "custom", "", "available",
                  "书房里的小事", anchor="In the study"),
            _slot("23:15", "23:59", "wind_down", "准备休息", "custom", "winding down…", "limited"),
        ]
    else:
        slots = [
            _slot("00:00", "06:30", "sleep", "在睡觉", "custom", "sleeping", "asleep"),
            _slot("06:30", "09:30", "slow_morning", "在家过一个安静的早晨", "custom", "", "available",
                  "周末早晨", anchor="A slow morning"),
            _slot("09:30", "12:30", "estate", "处理私人信件和家中事务", "playing", "Letters & household papers", "limited"),
            _slot("12:30", "15:00", "lunch_walk", "午餐后在伦敦散步", "watching", "London, unhurried", "available", "沿路看到的事"),
            _slot("15:00", "18:30", "archive", "在档案室或书房阅读", "playing", "Archive afternoon", "limited", "旧纸与书"),
            evening,
            _slot("21:00", "23:30", "study", "在书房听音乐", "listening", music, "available", "正在听的音乐"),
            _slot("23:30", "23:59", "wind_down", "准备休息", "custom", "winding down…", "limited"),
        ]
    return slots


def _wrap_schedule(slots: list[dict], trip_code: str) -> dict:
    """落库格式：带上行程代号，好让出发/返程当天认出旧日程已经作废。"""
    return {"trip": trip_code or "", "slots": slots}


def _unwrap_schedule(stored) -> tuple[list[dict], str]:
    """兼容早期只存一个 slot 列表的行：那时候还没有出差，一律当作在家。"""
    if isinstance(stored, str):
        stored = json.loads(stored)
    if isinstance(stored, list):
        return stored, ""
    if isinstance(stored, dict):
        slots = stored.get("slots")
        if isinstance(slots, list):
            return slots, str(stored.get("trip") or "")
    return [], ""


async def _load_or_create_schedule(now: datetime) -> list[dict]:
    day = now.date()
    trip_code = trips.trip_code()
    if (
        state.daily_life_date == day
        and state.daily_life_schedule
        and state.daily_life_trip == trip_code
    ):
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
                    stored_slots, stored_trip = _unwrap_schedule(row[0]) if row else ([], "")
                    if stored_slots and stored_trip == trip_code:
                        schedule = stored_slots
                    else:
                        # 没存过，或者出差状态变了（出发/返程）：当天剩下的时间换一套日程。
                        schedule = _build_schedule(now, trip_code)
                        await cur.execute(
                            "INSERT INTO daily_life_schedules(schedule_date, schedule_json) VALUES(%s, %s::jsonb) "
                            "ON CONFLICT(schedule_date) DO UPDATE "
                            "SET schedule_json=EXCLUDED.schedule_json, updated_at=NOW()",
                            (day, json.dumps(_wrap_schedule(schedule, trip_code), ensure_ascii=False)),
                        )
                        await conn.commit()
        except Exception as exc:
            print(f"⚠️ 每日日程读写失败，使用进程内日程: {exc}")
    if not schedule:
        schedule = _build_schedule(now, trip_code)
    state.daily_life_date = day
    state.daily_life_schedule = schedule
    state.daily_life_trip = trip_code
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


def slot_end_utc(slot: dict, now_local: datetime) -> datetime:
    """时段结束时刻。日程按他当地时间排，所以这里用他此刻所在地的时区。"""
    h, m = (int(x) for x in slot["end"].split(":"))
    end = datetime.combine(now_local.date(), time(h, m), tzinfo=now_local.tzinfo or LONDON)
    if end <= now_local:
        end += timedelta(days=1)
    return end.astimezone(timezone.utc)


def _transient_window_open(slot: dict, now_local: datetime) -> bool:
    """这个时段此刻是否该挂那句 AI 碎碎念。

    只在时段开头的 TRANSIENT_MINUTES 分钟内成立，过了就落回锚定活动；
    开不开由「日期＋时段」的固定种子决定，不会在同一个时段里忽有忽无。
    """
    if not presence.TRANSIENT_ENABLED or presence.CLEARED:
        return False
    if not slot.get("bubble") or slot.get("availability") == "asleep":
        return False
    minutes_in = (now_local.hour * 60 + now_local.minute) - _minutes(slot["start"])
    if not (0 <= minutes_in < presence.TRANSIENT_MINUTES):
        return False
    seed = f"{now_local.date().isoformat()}:{slot['key']}"
    return random.Random(seed).random() < presence.TRANSIENT_CHANCE


async def _bubble_text(slot: dict, schedule: list[dict], now: datetime) -> str:
    """取这个时段的碎碎念；一天只生成一次，之后从日程里读回来。"""
    text = slot.get("presence") or ""
    if text:
        return text
    text = await generate_custom_bubble()
    slot["presence"] = text
    if config.DATABASE_URL:
        try:
            async with _db.db_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE daily_life_schedules SET schedule_json=%s::jsonb, updated_at=NOW() "
                        "WHERE schedule_date=%s",
                        (json.dumps(_wrap_schedule(schedule, trips.trip_code()), ensure_ascii=False),
                         now.date()),
                    )
                    await conn.commit()
        except Exception as exc:
            print(f"⚠️ 气泡状态持久化失败: {exc}")
    return text


async def refresh_life_state(*, force_presence: bool = False, force_bubble: bool = False) -> tuple[dict, bool]:
    """刷新作息状态并把它反映到 Discord 状态栏。

    force_bubble=True 是 /状态栏设置 的「立刻换一个」：不管瞬时窗口开没开，
    都现生成一句新的碎碎念挂上去，否则同一个时段里重刷只会得到同一行字。
    """
    # 出差时这是目的地的当地时间，在家时就是伦敦时间。
    now = trips.local_now()
    schedule = await _load_or_create_schedule(now)
    slot = _current_slot(schedule, now)
    location = trips.current_location()
    transient = _transient_window_open(slot, now)
    if (
        force_bubble and presence.TRANSIENT_ENABLED and not presence.CLEARED
        and slot.get("bubble") and slot.get("availability") != "asleep"
    ):
        slot["presence"] = ""   # 丢掉今天缓存的那句，重新生成
        transient = True
    old = state.current_life_slot or {}
    # 换城市、气泡过掉都算「换了状态」：出发/返程和碎碎念到点都要立刻反映到状态栏。
    changed = (
        old.get("key") != slot.get("key")
        or (old.get("city") or "") != location["city_cn"]
        or bool(old.get("transient")) != transient
    )
    # 注意 slot 仍然是 schedule 里的那个 dict：下面生成气泡时要写回它再落库。
    state.current_life_slot = {
        **slot, "city": location["city_cn"], "is_trip": location["is_trip"], "transient": transient,
    }

    if slot.get("availability") == "busy":
        state.work_busy_activity = slot["label"]
        state.work_busy_until = slot_end_utc(slot, now)

    # 状态栏停掉或清空时，上面的作息状态照常更新——提示词里的「你此刻在做什么」
    # 和主动开口都读它，停的只是 Discord 那一栏。
    if presence.CLEARED or not presence.ROTATION_ENABLED:
        return state.current_life_slot, changed

    if changed or force_presence:
        if transient:
            kind = "custom"
            text = await _bubble_text(slot, schedule, now)
            state.current_life_slot["presence"] = text
        else:
            kind = slot.get("anchor_kind") or slot.get("kind", "playing")
            text = slot.get("anchor") or slot.get("presence") or "Quietly occupied"
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
            state.set_current_presence(
                kind, text, source=f"life:{slot['key']}",
                duration_type="instant" if transient else "sustained",
            )
            print(f"🗓️ 日程状态 → {slot['label']} [{kind}] {text}{'（瞬时）' if transient else ''}")
        except Exception as exc:
            print(f"⚠️ 日程 Presence 更新失败: {exc}")
    return state.current_life_slot, changed
