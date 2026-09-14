"""所有 @tasks.loop 后台任务，以及相关工具函数。"""
import asyncio
import json
import random
import re
import traceback as _tb
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks

import config
import state
import db as _db
from client import discord_client
from ai_client import ai_chat_create
from history import (
    get_history, trim_history, history_key_for,
    flush_dirty_histories, delete_persisted_history, _msg_to_plain_text,
)
from memory import fetch_due_reminders, delete_reminder, get_recall_candidate
from directives import parse_bot_directives
from presence import generate_presence, generate_custom_bubble, _TYPE_MAP
import presence as presence_mod
from followups import check_due_followups
from polls import check_ended_polls
from life_state import refresh_life_state
import trips

# ==== 可通过 /post_config 实时修改的参数 ====
DAILY_CARD_PROB_NORMAL = 0.35
DAILY_CARD_PROB_OCCASION = 0.85
STALE_POST_AGE_DAYS = 7
STALE_POST_MAX_REPLIES = 0
CLEANUP_INTERVAL_HOURS = 12
CLEANUP_ENABLED = True
DAILY_CARD_ENABLED = True

# 每日主动话题：在恋人指定的频道池里，每天主动找她聊一两句。
PROACTIVE_TOPIC_ENABLED = True
PROACTIVE_TOPIC_CHANNEL_IDS = list(dict.fromkeys(
    channel_id for channel_id in (config.PARTNER_HOME_CHANNEL_ID, config.PROACTIVE_CHANNEL_ID) if channel_id
))
PROACTIVE_TOPIC_MIN_DAILY = 1
PROACTIVE_TOPIC_MAX_DAILY = 1
PROACTIVE_TOPIC_START_HOUR = 10
PROACTIVE_TOPIC_END_HOUR = 23
PROACTIVE_TOPIC_MIN_GAP_HOURS = 4.0
PROACTIVE_TOPIC_PREFERENCE = ""
_proactive_topic_state: dict = {
    "date": "",
    "times": [],
    "sent": 0,
    "last_sent_at": "",
    "recent_topics": [],
    "channel_cursor": 0,
}

# ==== 主动私信（可通过 /proactive_dm 实时修改）====
PROACTIVE_DM_ENABLED = True          # 总开关，关掉后他完全不会主动私信
PROACTIVE_DM_INTERVAL_HOURS = 8      # 多久检查一次「要不要找她」
PROACTIVE_DM_CHANCE = 0.15           # 每次检查真正发出去的概率
PROACTIVE_DM_MIN_IDLE_MINUTES = 90   # 她安静满多久之后才考虑主动开口
PROACTIVE_DM_LATE_NIGHT = True       # 深夜（她那边 2~8 点）是否允许
PROACTIVE_DM_LATE_CHANCE = 0.03      # 深夜时段额外的一层概率

# ==== 每日卡片防重复 ====
_last_daily_card_date: str = ""

# ==== 每日摘要防重复 ====
_daily_summary_done_for: date | None = None


def _attach_loop_error_handler(loop_obj, name: str):
    async def _on_err(exc: BaseException):
        print(f"⚠️ 后台任务 {name} 异常：{type(exc).__name__}: {exc}")
        _tb.print_exception(type(exc), exc, exc.__traceback__)
        if not loop_obj.is_running():
            try:
                loop_obj.restart()
                print(f"♻️ 后台任务 {name} 已自动重启")
            except Exception as e:
                print(f"❌ 后台任务 {name} 重启失败：{e}")
    loop_obj.error(_on_err)


def should_opportunistic_post(user_text: str, is_partner: bool = False) -> bool:
    lowered = (user_text or "").lower()
    forum_keywords = ["发帖", "帖子", "论坛", "post", "forum", "thread", "开贴", "主楼", "置顶"]
    if any(k in lowered for k in forum_keywords):
        return random.random() < 0.08
    if is_partner:
        topic_keywords = [
            "推荐", "安利", "好看", "好听", "好玩", "在看", "在听", "最近",
            "发现", "觉得", "想说", "想分享", "值得", "必看", "必玩",
            "好用", "喜欢", "不错", "有意思", "有趣",
        ]
        if any(k in lowered for k in topic_keywords):
            return random.random() < 0.03
    return False


def parse_reminder_from_text(text: str) -> tuple[timedelta | None, str]:
    text = text.strip()
    patterns = [
        (r'(\d+(?:\.\d+)?)\s*(?:个)?小时后?', 'hours'),
        (r'(\d+(?:\.\d+)?)\s*(?:h|hr)后?', 'hours'),
        (r'(\d+)\s*(?:分钟|分|mins?)后?', 'minutes'),
        (r'(\d+)\s*(?:天|day)后?', 'days'),
    ]
    for pattern, unit in patterns:
        m = re.search(pattern, text)
        if m:
            val = float(m.group(1))
            if unit == 'hours':
                delta = timedelta(hours=val)
            elif unit == 'minutes':
                delta = timedelta(minutes=val)
            else:
                delta = timedelta(days=val)
            content = re.sub(r'提醒我?\s*' + pattern, '', text).strip()
            content = re.sub(r'^[，,。\s]+|[，,。\s]+$', '', content)
            if not content:
                content = text
            return delta, content
    return None, ""


def _extract_json_object(text: str) -> dict | None:
    """从可能包含多余文本的 AI 回复中提取第一个 JSON 对象。"""
    if not text:
        return None
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
    cleaned = re.sub(r'\s*```\s*$', '', cleaned).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    depth = 0
    start = -1
    for i, ch in enumerate(cleaned):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(cleaned[start:i + 1])
                except json.JSONDecodeError:
                    start = -1
    return None


async def generate_daily_card_data(weather: str | None = None) -> dict | None:
    from config import get_beijing_time_note, get_today_occasion
    time_ctx = get_beijing_time_note()
    occasion = get_today_occasion()
    occasion_hint = f"今天是{occasion}。" if occasion else ""
    location = trips.current_location()
    weather_hint = f"{location['city_cn']}实时天气：{weather}。" if weather else ""

    prompt = (
        f"{time_ctx}{trips.trip_hint_text()}\n{occasion_hint}{weather_hint}"
        "请为T.S.（Theodore Sinclair / 沈玘言）生成今日状态卡片的内容。\n"
        "以他的视角，用简短、克制的语言填写以下字段。\n\n"
        "【双语规则（极其重要）】\n"
        "每个字段必须采用「英文 — 中文」双语格式，用空格 + em dash + 空格连接。\n\n"
        "【字段说明（每个字段都要双语）】\n"
        f"location: 当前所在地（不超过30字，此刻你在{location['city_cn']}，必须与此一致）\n"
        "reading: 今天在读的书或文件，真实书名+作者（不超过30字）\n"
        "listening: 今天在听的音乐，艺术家+曲名/专辑（不超过30字）\n"
        "note: 今日一句话碎念/感受（不超过50字，双语）\n"
        "weather: 一句对天气的感受（不超过30字，双语）\n"
        "footer: 极短落款（不超过24字）\n\n"
        "【禁止】不要提到恋人；不要用励志/鸡汤语气；不要编造不存在的书名\n\n"
        "输出格式：严格只输出一个JSON对象，不加任何说明或代码块标记。\n"
        '示例：{"location":"...","reading":"...","listening":"...","note":"...","weather":"...","footer":"..."}\n\n'
        "现在输出："
    )

    messages = [
        {"role": "system", "content": "你是一个JSON生成助手。用户会描述需要的字段，你只输出一个合法的JSON对象，不加任何多余文字。"},
        {"role": "user", "content": prompt},
    ]

    for attempt in range(2):
        try:
            response = await ai_chat_create(
                model=config.MODEL_NAME,
                messages=messages,
                max_tokens=8192,
                temperature=0.7,
            )
            msg = response.choices[0].message if response.choices else None
            if not msg:
                print(f"⚠️ 生成今日卡片数据: API 无 choices (attempt {attempt + 1}/2)")
                continue
            raw = (msg.content or "").strip()
            if not raw:
                extra = ""
                for attr in ("reasoning_content", "reasoning", "refusal"):
                    val = getattr(msg, attr, None)
                    if val:
                        extra += f" {attr}={str(val)[:150]}"
                print(f"⚠️ 生成今日卡片数据: content为空 (attempt {attempt + 1}/2)"
                      f" finish_reason={response.choices[0].finish_reason}{extra}")
                continue
            data = _extract_json_object(raw)
            if data:
                return data
            print(f"⚠️ 生成今日卡片数据: JSON 解析失败 (attempt {attempt + 1}/2)，原始: {raw[:300]!r}")
        except Exception as e:
            print(f"⚠️ 生成今日卡片数据失败 (attempt {attempt + 1}/2): {e}")
        if attempt == 0:
            await asyncio.sleep(3)
    return None


# ==== @tasks.loop 后台任务 ====

@tasks.loop(hours=PROACTIVE_DM_INTERVAL_HOURS)
async def proactive_dm_partner():
    await discord_client.wait_until_ready()
    if not PROACTIVE_DM_ENABLED:
        return
    # 睡眠时段不主动发私信
    is_sleeping, sleep_phase = config.is_sleep_time()
    if state.current_life_slot:
        is_sleeping = state.current_life_slot.get("availability") == "asleep"
    if is_sleeping:
        return

    if state.last_partner_activity_at is not None:
        from datetime import timezone
        idle = (datetime.now(timezone.utc) - state.last_partner_activity_at).total_seconds()
        if idle < PROACTIVE_DM_MIN_IDLE_MINUTES * 60:
            return
    if random.random() > PROACTIVE_DM_CHANCE:
        return

    hour = datetime.now(ZoneInfo("Asia/Shanghai")).hour
    is_late_night = 2 <= hour < 8
    if is_late_night:
        if not PROACTIVE_DM_LATE_NIGHT:
            return
        if random.random() > PROACTIVE_DM_LATE_CHANCE:
            return

    await send_proactive_dm(is_late_night=is_late_night)


async def send_proactive_dm(*, is_late_night: bool = False) -> tuple[bool, str]:
    """真正把私信发出去。返回 (是否发出, 说明)，/proactive_dm 的『立即发送』也走这里。"""
    try:
        from datetime import timezone
        from ai_client import call_ai
        partner = await discord_client.fetch_user(config.PARTNER_USER_ID)
        # 出差时把位置一并交代清楚，免得他人在东京却说「伦敦今天下雨」。
        time_ctx = config.get_beijing_time_note() + trips.trip_hint_text()

        FORMAT_REMINDER = (
            "【⚠️格式硬约束（不允许妥协）】"
            "1) 每一条消息严格两行：第一行英文，第二行中文翻译用括号括起来。"
            "2) 多条消息之间必须用大写 [SPLIT] 单独占一行隔开。"
            "3) 整段输出里不允许出现 [REACTION:...]。"
        )
        if is_late_night:
            prompt_content = (
                f"（系统提示：{time_ctx} 现在是深夜。你还没睡，想到了恋人，给她发一条私信。"
                "内容极短，语气比平时更轻，带一点深夜特有的安静感。"
                f"绝对不要催她睡觉。{FORMAT_REMINDER}）{state.life_hint_text()}"
            )
        else:
            recall_note = None
            if random.random() < 0.30:
                recall_note = await get_recall_candidate(str(config.PARTNER_USER_ID))

            if recall_note:
                prompt_content = (
                    f"（系统提示：{time_ctx} 你主动给恋人发私信。"
                    f"你想到了她之前提过的一件事：「{recall_note}」。"
                    "请自然地以这件事为由头发一条消息。绝对不要说'我记得你说过'，"
                    f"直接当共同认知使用。私聊语气，简短。{FORMAT_REMINDER}）{state.life_hint_text()}"
                )
            else:
                prompt_content = (
                    f"（系统提示：{time_ctx} 你现在主动给恋人发了一条私信。"
                    "内容要符合你的风格。可以是随口问她在干嘛、说一句你在做什么。"
                    f"私聊语气比群里柔软，但依然不要油腻。{FORMAT_REMINDER}）{state.life_hint_text()}"
                )
        dm_key = f"dm:{config.PARTNER_USER_ID}"
        dm_hist = get_history(dm_key)
        temp_history = dm_hist.copy()
        temp_history.append({"role": "user", "content": prompt_content})
        raw_reply = await call_ai(temp_history)
        if "[IGNORE]" in raw_reply.upper():
            return False, "他这次选择不说话（[IGNORE]）"
        _, msgs, _, _, _ = parse_bot_directives(raw_reply)
        if not msgs:
            return False, "模型没有产出可发送的消息"
        for msg_text in msgs:
            await partner.send(msg_text)
            await asyncio.sleep(1.5)
        clean = "\n[SPLIT]\n".join(msgs)
        async with state.get_bucket_lock(dm_key):
            dm_hist.append({
                "role": "assistant",
                "content": f"（主动私信恋人）{clean.replace('[SPLIT]', chr(10))}"
            })
        await trim_history(dm_key)
        print(f"✅ 主动私信恋人发送成功（深夜模式: {is_late_night}）")
        return True, "已发出"
    except Exception as e:
        print(f"主动私信恋人报错: {e}")
        return False, str(e)[:150]


@tasks.loop(hours=1)
async def anniversary_check():
    await discord_client.wait_until_ready()
    if not config.DATABASE_URL:
        return
    now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))
    if now_bj.hour != 9:
        return

    try:
        from ai_client import call_ai
        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT id, note, category, event_date, anniversary_last_year
                       FROM user_notes
                       WHERE user_id=%s
                         AND event_date IS NOT NULL
                         AND EXTRACT(MONTH FROM event_date)=%s
                         AND EXTRACT(DAY FROM event_date)=%s
                         AND (anniversary_last_year IS NULL OR anniversary_last_year < %s)""",
                    (str(config.PARTNER_USER_ID), now_bj.month, now_bj.day, now_bj.year),
                )
                hits = await cur.fetchall()
            if not hits:
                return

            nid, note, cat, _ed, _last = hits[0]
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE user_notes SET anniversary_last_year=%s, recalled_at=NOW(), recall_count=recall_count+1 WHERE id=%s",
                    (now_bj.year, nid),
                )
                await conn.commit()

        partner = await discord_client.fetch_user(config.PARTNER_USER_ID)
        # 出差时把位置一并交代清楚，免得他人在东京却说「伦敦今天下雨」。
        time_ctx = config.get_beijing_time_note() + trips.trip_hint_text()
        FORMAT_REMINDER = (
            "【⚠️格式硬约束】每条严格两行：第一行英文，第二行中文翻译括起来，"
            "多条之间用 [SPLIT] 单独占一行分隔。不允许出现 [REACTION:...]。"
        )
        is_birthday = (cat == "日期" and "生日" in (note or ""))
        if is_birthday:
            tone = ("今天是恋人的生日。给她发一条私信，语气是你的风格：克制、不甜腻、但有心。")
        else:
            tone = (f"今天是这件事的纪念日/重要日子：「{note}」。给恋人发一条私信轻轻提一下，简短。")
        prompt_content = f"（系统提示：{time_ctx} {tone} {FORMAT_REMINDER}）"

        dm_key = f"dm:{config.PARTNER_USER_ID}"
        dm_hist = get_history(dm_key)
        temp_history = dm_hist.copy()
        temp_history.append({"role": "user", "content": prompt_content})
        raw_reply = await call_ai(temp_history)
        if "[IGNORE]" in raw_reply.upper():
            return
        _, msgs, _, _, _ = parse_bot_directives(raw_reply)
        if not msgs:
            return
        for msg_text in msgs:
            await partner.send(msg_text)
            await asyncio.sleep(1.5)
        clean = "\n[SPLIT]\n".join(msgs)
        async with state.get_bucket_lock(dm_key):
            dm_hist.append({
                "role": "assistant",
                "content": f"（纪念日私信恋人：{note}）{clean.replace('[SPLIT]', chr(10))}"
            })
        await trim_history(dm_key)
        print(f"✅ 纪念日触发成功：{note}")
    except Exception as e:
        print(f"⚠️ 纪念日触发失败: {e}")


@tasks.loop(hours=1)
async def daily_summary_task():
    global _daily_summary_done_for
    await discord_client.wait_until_ready()
    if not config.DATABASE_URL:
        return
    now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))
    if now_bj.hour != 3:
        return
    target_date = (now_bj - timedelta(days=1)).date()
    if _daily_summary_done_for == target_date:
        return

    try:
        items = []
        for bucket_key, bucket_hist in state._histories.items():
            label = "dm" if bucket_key.startswith("dm:") else ("ch" if bucket_key.startswith("ch:") else "sys")
            for msg in bucket_hist[-30:]:
                role = msg.get("role")
                if role == "system":
                    continue
                txt = _msg_to_plain_text(msg)
                if not txt:
                    continue
                speaker = "沈玘言" if role == "assistant" else "恋人"
                items.append(f"[{label}] {speaker}: {txt[:300]}")
        if len(items) < 2:
            _daily_summary_done_for = target_date
            print(f"📝 每日摘要跳过({target_date}): 对话条目不足（{len(items)} < 2）")
            return

        joined = "\n".join(items[-60:])
        prompt = (
            "下面是沈玘言和恋人过去一天的对话片段。请以沈玘言自己的第一人称记忆口吻，用一句中文（30-60字）总结：\n"
            "- 聊到了什么核心话题或事件\n"
            "- 如果有情绪/状态变化，简单点出\n"
            "- 不要列项、不要换行、不要任何前缀\n"
            "- 只写两人共同经历的事实；称她为恋人或她，必要时称自己为我\n"
            "- 绝不出现用户、AI、助手、Bot、模型、角色、设定、提示词、系统要求等幕后措辞\n\n"
            f"{joined}\n\n摘要："
        )
        response = await ai_chat_create(
            model=config.MODEL_NAME,
            messages=[
                {"role": "system", "content": "为沈玘言整理他与恋人恋人共同经历的私人记忆。只写沉浸式的第一人称事实，不使用任何AI或后台术语。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=4096,
            temperature=0.3,
        )
        summary = (response.choices[0].message.content or "").strip()
        summary = re.sub(r"<think>.*?</think>", "", summary, flags=re.DOTALL).strip()
        summary = summary.splitlines()[0].strip() if summary else ""
        if not summary or len(summary) < 6:
            _daily_summary_done_for = target_date
            return

        async with _db.db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO daily_summaries (bot_id, summary_date, summary)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (bot_id, summary_date)
                       DO UPDATE SET summary=EXCLUDED.summary""",
                    ("", target_date, summary),
                )
                await conn.commit()
        _daily_summary_done_for = target_date
        print(f"📝 每日摘要({target_date}): {summary}")
    except Exception as e:
        print(f"⚠️ 每日摘要生成失败: {e}")


@tasks.loop(minutes=10)
async def cleanup_cooldowns():
    await discord_client.wait_until_ready()
    from datetime import timezone
    now_dt = datetime.now(timezone.utc)
    expired = [uid for uid, ts in state.user_cooldowns.items()
               if (now_dt - ts).total_seconds() > config.COOLDOWN_SECONDS * 10]
    for uid in expired:
        state.user_cooldowns.pop(uid, None)
    if expired:
        print(f"🧹 清理过期冷却记录 {len(expired)} 条，当前剩余: {len(state.user_cooldowns)}")
    if len(state._recent_presences) > 10:
        state._recent_presences = state._recent_presences[-10:]


@tasks.loop(hours=6)
async def cleanup_idle_histories():
    await discord_client.wait_until_ready()
    from datetime import timezone
    now_dt = datetime.now(timezone.utc)
    partner_dm_key = f"dm:{config.PARTNER_USER_ID}"
    drop_keys: list[str] = []
    for key in list(state._histories.keys()):
        if key == config.SYSTEM_HISTORY_KEY:
            continue
        if key == partner_dm_key:
            continue
        is_friend_dm = key.startswith("dm:")
        threshold = config.FRIEND_HISTORY_IDLE_DAYS * 86400 if is_friend_dm else config.HISTORY_IDLE_DAYS * 86400
        last = state._bucket_touched.get(key)
        if last is None:
            state._bucket_touched[key] = now_dt
            continue
        if (now_dt - last).total_seconds() > threshold:
            drop_keys.append(key)
    for k in drop_keys:
        state._histories.pop(k, None)
        state._bucket_locks.pop(k, None)
        state._bucket_touched.pop(k, None)
        await delete_persisted_history(k)
    if drop_keys:
        print(f"🧹 闲置桶清理：丢弃 {len(drop_keys)} 个，剩 {len(state._histories)} 个")


@tasks.loop(seconds=30)
async def persist_histories_task():
    await discord_client.wait_until_ready()
    await flush_dirty_histories()


# ==== 私人频道每日主动话题 ====

def build_proactive_topic_plan(now_bj: datetime) -> dict:
    count = random.randint(PROACTIVE_TOPIC_MIN_DAILY, PROACTIVE_TOPIC_MAX_DAILY)
    start_minute = PROACTIVE_TOPIC_START_HOUR * 60
    end_minute = PROACTIVE_TOPIC_END_HOUR * 60
    if count <= 0:
        times: list[str] = []
    elif count == 1:
        minute = random.randrange(start_minute, end_minute)
        times = [f"{minute // 60:02d}:{minute % 60:02d}"]
    else:
        minimum_gap = int(PROACTIVE_TOPIC_MIN_GAP_HOURS * 60)
        slack = max(0, (end_minute - start_minute - 1) - minimum_gap * (count - 1))
        offsets = sorted(random.uniform(0, slack) for _ in range(count))
        minutes = [start_minute + i * minimum_gap + int(offsets[i]) for i in range(count)]
        times = [f"{minute // 60:02d}:{minute % 60:02d}" for minute in minutes]
    previous = _proactive_topic_state or {}
    return {
        "date": now_bj.date().isoformat(),
        "times": times,
        "sent": 0,
        "last_sent_at": "",
        "recent_topics": list(previous.get("recent_topics") or [])[-5:],
        "channel_cursor": int(previous.get("channel_cursor", 0)),
    }


async def ensure_proactive_topic_plan(now_bj: datetime) -> None:
    global _proactive_topic_state
    if (
        _proactive_topic_state.get("date") == now_bj.date().isoformat()
        and isinstance(_proactive_topic_state.get("times"), list)
    ):
        return
    _proactive_topic_state = build_proactive_topic_plan(now_bj)
    from memory import save_persisted_config
    await save_persisted_config({
        "PROACTIVE_TOPIC_STATE": json.dumps(_proactive_topic_state, ensure_ascii=False),
    })
    print(f"🗓️ T.S. 今日主动话题计划: {_proactive_topic_state['times'] or '无'}")


async def send_proactive_topic(*, count_toward_plan: bool = True) -> tuple[bool, str]:
    """以私人恋人语气主动找恋人聊天，并在频道池中轮换发送。"""
    global _proactive_topic_state
    channel_ids = list(dict.fromkeys(PROACTIVE_TOPIC_CHANNEL_IDS))
    if not channel_ids:
        return False, "未配置目标频道"
    cursor = int((_proactive_topic_state or {}).get("channel_cursor", 0))
    channel = None
    selected_index = 0
    failures: list[str] = []
    for offset in range(len(channel_ids)):
        index = (cursor + offset) % len(channel_ids)
        channel_id = channel_ids[index]
        try:
            candidate = await discord_client.fetch_channel(channel_id)
            if not isinstance(candidate, discord.TextChannel):
                failures.append(f"{channel_id}:非文字频道")
                continue
            me = candidate.guild.me
            permissions = candidate.permissions_for(me) if me else None
            if permissions and not (permissions.view_channel and permissions.send_messages):
                failures.append(f"{channel_id}:权限不足")
                continue
            channel = candidate
            selected_index = index
            break
        except Exception as channel_error:
            failures.append(f"{channel_id}:{type(channel_error).__name__}")
    if channel is None:
        return False, "没有可发送的频道（" + "；".join(failures) + "）"

    preference = PROACTIVE_TOPIC_PREFERENCE.strip()
    preference_hint = (
        f"\n恋人希望你主动聊天时更偏向：<topic_preference>{preference}</topic_preference>。"
        if preference else ""
    )
    recent_topics = list((_proactive_topic_state or {}).get("recent_topics") or [])[-5:]
    recent_hint = ""
    if recent_topics:
        recent_hint = "\n你最近已经这样主动找过她，今天换个不同的念头：\n- " + "\n- ".join(recent_topics)
    prompt = (
        f"（系统隐秘提示：{config.get_beijing_time_note()} 你是沈玘言，现在忽然想在你和恋人恋人共用的频道里主动找她说话。"
        "这不是面向服务器成员的公共话题，也不是主持聊天；你只是在找自己的恋人。"
        "像真人想起爱人时那样自然开口：可以分享此刻正在做的事或突然想到的细节，"
        "延伸你们之间值得聊的小问题，带一点只对她有的偏爱、惦记或克制的亲密感。"
        "不要说‘大家’、不要欢迎新人、不要像机器人打卡、不要提及排程或后台，也不要 @她。"
        "只输出一条能直接发给恋人的消息，1到3句，继续遵守你原本的双语格式；"
        "不允许输出 [IGNORE]、[ACTION]、标题或解释。"
        f"{state.life_hint_text()}{preference_hint}{recent_hint}）"
    )
    try:
        from ai_client import call_ai
        hist_key = history_key_for(channel=channel)
        temp_history = get_history(hist_key).copy()
        temp_history.append({"role": "user", "content": prompt})
        raw = await call_ai(temp_history)
        if not raw or "[IGNORE]" in raw.upper() or "[ACTION]" in raw.upper():
            return False, "模型没有生成可发送的话题"
        _, messages, _, _, _ = parse_bot_directives(raw)
        message_text = next((message.strip() for message in messages if message.strip()), "")
        if not message_text:
            return False, "模型返回了空内容"
        message_text = message_text[:1900]
        await channel.send(message_text)

        async with state.get_bucket_lock(hist_key):
            get_history(hist_key).append({"role": "assistant", "content": message_text})
            state.mark_history_dirty(hist_key)
        await trim_history(hist_key)

        recent_topics.append(message_text[:300])
        _proactive_topic_state["recent_topics"] = recent_topics[-5:]
        _proactive_topic_state["last_sent_at"] = datetime.now(timezone.utc).isoformat()
        _proactive_topic_state["channel_cursor"] = (selected_index + 1) % len(channel_ids)
        if count_toward_plan:
            _proactive_topic_state["sent"] = int(_proactive_topic_state.get("sent", 0)) + 1
        from memory import save_persisted_config
        await save_persisted_config({
            "PROACTIVE_TOPIC_STATE": json.dumps(_proactive_topic_state, ensure_ascii=False),
        })
        print(f"✅ T.S. 主动话题已发送到 #{channel.name}: {message_text[:100]}")
        return True, f"已发送到 {channel.mention}"
    except Exception as e:
        print(f"⚠️ T.S. 主动话题发送失败: {type(e).__name__}: {e}")
        return False, f"{type(e).__name__}: {e}"


@tasks.loop(minutes=15)
async def proactive_topic_check():
    await discord_client.wait_until_ready()
    if not PROACTIVE_TOPIC_ENABLED or not PROACTIVE_TOPIC_CHANNEL_IDS:
        return
    now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))
    await ensure_proactive_topic_plan(now_bj)
    if not (PROACTIVE_TOPIC_START_HOUR <= now_bj.hour < PROACTIVE_TOPIC_END_HOUR):
        return
    if (state.current_life_slot or {}).get("availability") == "asleep":
        return
    planned = list(_proactive_topic_state.get("times") or [])
    sent = int(_proactive_topic_state.get("sent", 0))
    due_count = sum(1 for planned_time in planned if planned_time <= now_bj.strftime("%H:%M"))
    if sent >= due_count or sent >= len(planned):
        return
    last_raw = str(_proactive_topic_state.get("last_sent_at") or "")
    if last_raw:
        try:
            last_sent = datetime.fromisoformat(last_raw)
            if datetime.now(timezone.utc) - last_sent < timedelta(hours=PROACTIVE_TOPIC_MIN_GAP_HOURS):
                return
        except ValueError:
            pass
    await send_proactive_topic(count_toward_plan=True)


@tasks.loop(hours=1)
async def daily_occasion_check():
    await discord_client.wait_until_ready()
    if not config.PROACTIVE_CHANNEL_ID:
        return

    now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))
    today_key = now_bj.strftime("%m-%d")

    if state._last_occasion_date == today_key:
        return
    if not (9 <= now_bj.hour < 12):
        return

    occasion = config.get_today_occasion()
    if not occasion:
        return

    is_partner_birthday = (occasion == "恋人的生日")
    if not is_partner_birthday and random.random() > 0.80:
        state._last_occasion_date = today_key
        return

    state._last_occasion_date = today_key
    try:
        from ai_client import call_ai
        channel = await discord_client.fetch_channel(config.PROACTIVE_CHANNEL_ID)
        # 出差时把位置一并交代清楚，免得他人在东京却说「伦敦今天下雨」。
        time_ctx = config.get_beijing_time_note() + trips.trip_hint_text()

        if is_partner_birthday:
            prompt = (
                f"（系统提示：{time_ctx} 今天是恋人的生日（12月10日）。"
                "你在群里提一句——可以很短，可以含蓄，但不能假装不知道。"
                "风格保持克制，不要煽情，不要写祝福语套话。双语格式。）"
            )
        else:
            prompt = (
                f"（系统提示：{time_ctx} 今天是{occasion}。你注意到了这个日子。"
                "请在公屏发一条极短的、符合你风格的消息，也可以什么都不说 [IGNORE]。"
                "不要刻意，不要煽情，保持克制。双语格式。）"
            )

        ch_key = history_key_for(channel=channel)
        temp = get_history(ch_key).copy()
        temp.append({"role": "user", "content": prompt})
        raw = await call_ai(temp)
        if "[IGNORE]" in raw.upper():
            print(f"📅 节日感知（{occasion}）：AI 选择无视")
            return
        clean = re.sub(r'\[REACTION:.*?\]\n?', '', raw, flags=re.DOTALL)
        clean = re.sub(r'\[ACTION\].*?\[/ACTION\]\n?', '', clean, flags=re.DOTALL).strip()
        msgs = [m.strip() for m in clean.split('[SPLIT]') if m.strip()]
        for msg in msgs:
            await channel.send(msg)
        print(f"✅ 节日/重要日期发言: {occasion}")
    except Exception as e:
        print(f"⚠️ 节日发言报错: {e}")


@tasks.loop(hours=1)
async def daily_status_card():
    global _last_daily_card_date
    await discord_client.wait_until_ready()
    if not config.PROACTIVE_CHANNEL_ID:
        return

    # 卡片按他当地时间的早上 9 点发；出差时跟着目的地走。
    now_local = trips.local_now()
    if now_local.hour != 9:
        return

    today_key = now_local.strftime("%Y-%m-%d")
    if _last_daily_card_date == today_key:
        return
    _last_daily_card_date = today_key

    if not DAILY_CARD_ENABLED:
        return

    occasion = config.get_today_occasion()
    post_probability = DAILY_CARD_PROB_OCCASION if occasion else DAILY_CARD_PROB_NORMAL
    if random.random() > post_probability:
        print(f"🫥 今日状态卡片：今天没兴致发 (p={post_probability})")
        return

    try:
        from presence import get_city_weather
        location = trips.current_location()
        weather = await get_city_weather(location["city_en"])
        data = await generate_daily_card_data(weather=weather)
        if not data:
            return

        now_local = trips.local_now()
        weekday_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][now_local.weekday()]

        embed = discord.Embed(
            title=f"— {now_local.strftime('%B %d')} · {weekday_en} —",
            color=discord.Color(0x1e2330),
        )
        if data.get("location"):
            embed.add_field(name="📍", value=data["location"], inline=False)
        if data.get("reading"):
            embed.add_field(name="📖", value=data["reading"], inline=True)
        if data.get("listening"):
            embed.add_field(name="🎵", value=data["listening"], inline=True)
        if data.get("weather"):
            embed.add_field(name="🌫️", value=data["weather"], inline=True)
        if data.get("note"):
            embed.add_field(name="​", value=f"*{data['note']}*", inline=False)
        embed.set_footer(text=data.get("footer", "T.S."))

        channel = await discord_client.fetch_channel(config.PROACTIVE_CHANNEL_ID)
        thread_name = f"{now_local.strftime('%B %d')} · {weekday_en}"
        if isinstance(channel, discord.ForumChannel):
            await channel.create_thread(name=thread_name, embed=embed, auto_archive_duration=1440)
        else:
            await channel.send(embed=embed)
        print(f"✅ 今日状态卡片已发送: {today_key}")
    except Exception as e:
        print(f"⚠️ 发送今日状态卡片失败: {e}")


async def do_cleanup_stale_forum_posts(
    age_days: int | None = None,
    max_replies: int | None = None,
    dry_run: bool = False,
) -> dict:
    age = STALE_POST_AGE_DAYS if age_days is None else age_days
    maxr = STALE_POST_MAX_REPLIES if max_replies is None else max_replies
    from datetime import timezone

    if not config.PROACTIVE_CHANNEL_ID:
        return {"scanned": 0, "deleted": 0, "candidates": [], "reason": "未配置 PROACTIVE_CHANNEL_ID"}
    try:
        channel = await discord_client.fetch_channel(config.PROACTIVE_CHANNEL_ID)
    except Exception as e:
        return {"scanned": 0, "deleted": 0, "candidates": [], "reason": f"拉取频道失败: {e}"}
    if not isinstance(channel, discord.ForumChannel):
        return {"scanned": 0, "deleted": 0, "candidates": [], "reason": "目标频道不是论坛频道"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=age)
    threads: list[discord.Thread] = list(channel.threads)
    try:
        async for t in channel.archived_threads(limit=100):
            threads.append(t)
    except Exception:
        pass

    candidates: list[tuple[int, str]] = []
    deleted = 0
    scanned = 0
    for thread in threads:
        scanned += 1
        try:
            created = thread.created_at or discord.utils.snowflake_time(thread.id)
            if created and created > cutoff:
                continue
            if thread.owner_id and thread.owner_id != discord_client.user.id:
                continue
            non_bot_replies = 0
            async for msg in thread.history(limit=20, oldest_first=True):
                if msg.id == thread.id:
                    continue
                if msg.author and msg.author.id != discord_client.user.id and not msg.author.bot:
                    non_bot_replies += 1
                    if non_bot_replies > maxr:
                        break
            if non_bot_replies > maxr:
                continue
            candidates.append((thread.id, thread.name))
            if dry_run:
                continue
            await thread.delete()
            deleted += 1
            print(f"🧹 删除冷清旧帖: {thread.name} (id={thread.id})")
            await asyncio.sleep(1.5)
        except Exception as e:
            print(f"⚠️ 清理帖子失败 ({thread.id}): {e}")
    if deleted:
        print(f"🧹 本次清理共删除 {deleted} 个旧帖")
    return {"scanned": scanned, "deleted": deleted, "candidates": candidates, "reason": None}


@tasks.loop(hours=12)
async def cleanup_stale_forum_posts():
    await discord_client.wait_until_ready()
    if not CLEANUP_ENABLED:
        return
    await do_cleanup_stale_forum_posts()


@tasks.loop(minutes=1)
async def check_reminders():
    await discord_client.wait_until_ready()
    from datetime import timezone
    from ai_client import call_ai
    now = datetime.now(timezone.utc)
    due = await fetch_due_reminders(now)
    for r in due:
        try:
            if r.get("channel_id"):
                ch = await discord_client.fetch_channel(r["channel_id"])
                target_user = await discord_client.fetch_user(r["user_id"])
                reminder_prompt = (
                    f"（系统提示：你之前答应提醒 {target_user.display_name} 的时间到了。"
                    f"提醒内容：「{r['content']}」。"
                    "请用你的风格发出这条提醒，可以在消息里@对方，简短即可。双语格式。）"
                )
                temp_history = get_history(history_key_for(channel=ch)).copy()
                temp_history.append({"role": "user", "content": reminder_prompt})
                raw = await call_ai(temp_history)
                clean = re.sub(r'\[REACTION:.*?\]\n?', '', raw, flags=re.DOTALL)
                clean = re.sub(r'\[ACTION\].*?\[/ACTION\]\n?', '', clean, flags=re.DOTALL).strip()
                msgs = [m.strip() for m in clean.split('[SPLIT]') if m.strip()]
                mention = f"<@{r['user_id']}>"
                for i, msg_text in enumerate(msgs):
                    if i == 0 and mention not in msg_text:
                        msg_text = f"{mention} {msg_text}"
                    await ch.send(msg_text)
            else:
                user = await discord_client.fetch_user(r["user_id"])
                reminder_prompt = (
                    f"（系统提示：你之前答应提醒恋人的时间到了。提醒内容：「{r['content']}」。"
                    "请用你的私信风格发出这条提醒，简短，双语格式。）"
                )
                temp_history = get_history(f"dm:{r['user_id']}").copy()
                temp_history.append({"role": "user", "content": reminder_prompt})
                raw = await call_ai(temp_history)
                clean = re.sub(r'\[REACTION:.*?\]\n?', '', raw, flags=re.DOTALL)
                clean = re.sub(r'\[ACTION\].*?\[/ACTION\]\n?', '', clean, flags=re.DOTALL).strip()
                msgs = [m.strip() for m in clean.split('[SPLIT]') if m.strip()]
                for msg_text in msgs:
                    await user.send(msg_text)
            # 仅在成功发送后才删除；失败则保留，下一轮（1 分钟后）重试
            await delete_reminder(r)
            print(f"✅ 提醒已发送: {r['content']}")
        except Exception as e:
            print(f"⚠️ 发送提醒失败（保留待下轮重试）: {e}")


PRESENCE_INSTANT_MIN_SEC = 60
PRESENCE_INSTANT_MAX_SEC = 3 * 60
PRESENCE_SUSTAINED_MIN_SEC = 20 * 60
PRESENCE_SUSTAINED_MAX_SEC = 45 * 60


def _should_rotate_presence() -> bool:
    if state.current_presence is None:
        return True
    since = state.current_presence.get("since")
    if not isinstance(since, datetime):
        return True
    elapsed = (datetime.now(timezone.utc) - since).total_seconds()
    dur_type = state.current_presence.get("duration_type", "sustained")
    if dur_type == "instant":
        threshold = random.uniform(PRESENCE_INSTANT_MIN_SEC, PRESENCE_INSTANT_MAX_SEC)
    else:
        threshold = random.uniform(PRESENCE_SUSTAINED_MIN_SEC, PRESENCE_SUSTAINED_MAX_SEC)
    return elapsed >= threshold


@tasks.loop(hours=trips.TRIP_CHECK_INTERVAL_HOURS)
async def trip_scheduler_check():
    """结束到期的出差，并偶尔开始一趟新的。"""
    await discord_client.wait_until_ready()
    await trips.scheduler_tick()


async def _wakeup_catchup():
    """睡醒后补回复：把睡眠期间收到的消息打包，按频道分组，逐个补回复。"""
    pending = list(state.sleep_pending_messages)
    state.sleep_pending_messages.clear()
    if not pending:
        return

    await asyncio.sleep(random.uniform(60, 180))

    from ai_client import call_ai
    from reply import send_ai_reply

    by_channel: dict[int, list[dict]] = {}
    for entry in pending:
        by_channel.setdefault(entry["channel_id"], []).append(entry)

    for ch_id, entries in by_channel.items():
        try:
            channel = await discord_client.fetch_channel(ch_id)

            lines = []
            for e in entries:
                status = "你迷迷糊糊回了一句" if e["drowsy"] else "你只挂了个睡觉表情"
                lines.append(f"- {e['display_name']} 说：「{e['text']}」（{status}）")
            summary = "\n".join(lines)

            last_entry = entries[-1]
            last_message = None
            try:
                last_message = await channel.fetch_message(last_entry["message_id"])
            except Exception:
                pass

            # 出差时把位置一并交代清楚，免得他人在东京却说「伦敦今天下雨」。
            time_ctx = config.get_beijing_time_note() + trips.trip_hint_text()
            FORMAT_REMINDER = (
                "【⚠️格式硬约束（不允许妥协）】"
                "1) 每一条消息严格两行：第一行英文，第二行中文翻译用括号括起来。"
                "2) 多条消息之间必须用大写 [SPLIT] 单独占一行隔开。"
                "3) 整段输出里不允许出现 [REACTION:...]。"
            )
            prompt = (
                f"（系统提示：{time_ctx} 你刚睡醒，拿起手机看到睡着的时候收到了这些消息：\n"
                f"{summary}\n\n"
                "你现在要像真人睡醒看手机一样，回去补回复这些消息。"
                "语气自然，像是刚醒来翻手机然后逐条回——可以带一点刚睡醒的慵懒感，但已经清醒了。"
                "如果之前迷迷糊糊回过，可以补充、纠正、或接着之前的话继续。"
                "如果只是挂了表情没回，现在要正式回复内容。"
                f"分多段用 [SPLIT] 隔开。{FORMAT_REMINDER}）"
            )

            if last_entry["is_dm"]:
                hist_key = f"dm:{last_entry['user_id']}"
            else:
                hist_key = history_key_for(channel=channel)

            hist = get_history(hist_key)
            temp_history = hist.copy()
            temp_history.append({"role": "user", "content": prompt})

            raw_reply = await call_ai(temp_history)
            if "[IGNORE]" in raw_reply.upper():
                continue

            if last_message:
                await send_ai_reply(raw_reply, last_message, channel)
            else:
                _, msgs, _, _, _ = parse_bot_directives(raw_reply)
                for msg_text in msgs:
                    await channel.send(msg_text)
                    await asyncio.sleep(1.5)

            print(f"✅ 睡醒补回复: 频道 {ch_id}，{len(entries)} 条待处理")
        except Exception as e:
            print(f"⚠️ 睡醒补回复失败 (频道 {ch_id}): {e}")


@tasks.loop(minutes=presence_mod.ROTATE_MINUTES)
async def rotate_presence():
    await discord_client.wait_until_ready()
    previous = state.current_life_slot or {}
    previous_availability = previous.get("availability")
    slot, changed = await refresh_life_state()

    if previous_availability == "busy" and slot.get("availability") != "busy":
        state.work_busy_until = None
        state.work_busy_activity = ""
    elif slot.get("availability") != "busy":
        state.work_busy_until = None
        state.work_busy_activity = ""

    if previous_availability == "asleep" and slot.get("availability") != "asleep":
        if state.sleep_pending_messages:
            state.spawn_bg(_wakeup_catchup(), name="wakeup_catchup")


@tasks.loop(minutes=30)
async def commitment_followup_check():
    await discord_client.wait_until_ready()
    await check_due_followups()


@tasks.loop(minutes=5)
async def poll_followup_check():
    await discord_client.wait_until_ready()
    await check_ended_polls()


# ==== 随机论坛发帖 ====
RANDOM_POST_PROB = 0.20
RANDOM_POST_INTERVAL_HOURS = 4
_last_random_post_date: str = ""

@tasks.loop(hours=4)
async def random_forum_post():
    global _last_random_post_date
    await discord_client.wait_until_ready()
    if not config.PROACTIVE_CHANNEL_ID:
        return

    now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))
    if not (10 <= now_bj.hour <= 23):
        return

    today_key = now_bj.strftime("%Y-%m-%d")
    if _last_random_post_date == today_key:
        return

    if random.random() > RANDOM_POST_PROB:
        return

    _last_random_post_date = today_key

    try:
        from ai_client import call_ai
        channel = await discord_client.fetch_channel(config.PROACTIVE_CHANNEL_ID)
        if not isinstance(channel, discord.ForumChannel):
            return

        # 出差时把位置一并交代清楚，免得他人在东京却说「伦敦今天下雨」。
        time_ctx = config.get_beijing_time_note() + trips.trip_hint_text()
        prompt = (
            f"（系统提示：{time_ctx} 你现在打开了自己的论坛频道，想随手发一个帖子。\n"
            f"{state.life_hint_text()}\n"
            "帖子主题可以是你最近在思考的事、看到的东西、读的书、听的音乐、"
            "某个回忆、一段观察、一个无聊的想法，或者任何你觉得值得写两句的东西。\n"
            "风格保持克制、真实，不要鸡汤，不要太长。双语格式。\n"
            "不要提到恋人。\n\n"
            "输出格式：严格只输出一个JSON对象，包含两个字段：\n"
            '- "title": 帖子标题（简短，10-30字，双语）\n'
            '- "content": 帖子正文（50-200字，双语）\n'
            '示例：{"title":"...","content":"..."}\n'
            "现在输出："
        )
        response = await ai_chat_create(
            model=config.MODEL_NAME,
            messages=[
                {"role": "system", "content": "你是一个JSON生成助手。用户会描述需要的字段，你只输出一个合法的JSON对象，不加任何多余文字。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=8192,
            temperature=0.85,
        )
        raw = (response.choices[0].message.content or "").strip()
        data = _extract_json_object(raw)
        if not data or "title" not in data or "content" not in data:
            print(f"⚠️ 随机发帖: AI 返回无效数据: {raw[:200]!r}")
            return

        thread, first_msg = await channel.create_thread(
            name=data["title"],
            content=data["content"],
            auto_archive_duration=1440,
        )
        await asyncio.sleep(1.0)
        _auto_emojis = ["📖", "🖊️", "🩶", "🫡", "📌", "💭", "☕", "🌙"]
        try:
            await first_msg.add_reaction(random.choice(_auto_emojis))
        except Exception:
            pass
        print(f"✅ 随机论坛发帖: {data['title']}")
    except Exception as e:
        print(f"⚠️ 随机论坛发帖失败: {e}")


# ==== 论坛帖子互动（反应+评论）====
@tasks.loop(hours=3)
async def forum_interaction():
    await discord_client.wait_until_ready()
    if not config.PROACTIVE_CHANNEL_ID:
        return

    now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))
    if not (9 <= now_bj.hour <= 23):
        return

    if random.random() > 0.35:
        return

    try:
        channel = await discord_client.fetch_channel(config.PROACTIVE_CHANNEL_ID)
        if not isinstance(channel, discord.ForumChannel):
            return

        threads = list(channel.threads)
        if not threads:
            return

        recent_threads = sorted(
            threads,
            key=lambda t: t.created_at or discord.utils.snowflake_time(t.id),
            reverse=True,
        )[:10]

        target = random.choice(recent_threads)

        messages = []
        async for msg in target.history(limit=5, oldest_first=True):
            messages.append(msg)
        if not messages:
            return

        action = random.choice(["react", "react", "comment"])

        if action == "react":
            eligible = [m for m in messages if m.author.id != discord_client.user.id]
            if not eligible:
                eligible = messages[:1]
            msg_to_react = random.choice(eligible)
            react_emojis = ["👀", "🩶", "📖", "☕", "💭", "🌙", "✨", "🫡", "🖊️"]
            try:
                await msg_to_react.add_reaction(random.choice(react_emojis))
                print(f"✅ 论坛互动: 对帖子 [{target.name}] 添加反应")
            except Exception as e:
                print(f"⚠️ 论坛互动反应失败: {e}")

        elif action == "comment":
            from ai_client import call_ai
            first_msg = messages[0]
            post_content = first_msg.content or "(embed/无文字)"
            recent_replies = ""
            if len(messages) > 1:
                reply_parts = []
                for m in messages[1:]:
                    author = "T.S." if m.author.id == discord_client.user.id else m.author.display_name
                    reply_parts.append(f"{author}: {m.content[:100]}")
                recent_replies = "\n最近的回复：\n" + "\n".join(reply_parts)

            # 出差时把位置一并交代清楚，免得他人在东京却说「伦敦今天下雨」。
            time_ctx = config.get_beijing_time_note() + trips.trip_hint_text()
            prompt = (
                f"（系统提示：{time_ctx} 你在论坛上看到一个帖子。\n"
                f"标题：{target.name}\n"
                f"内容：{post_content[:300]}\n"
                f"{recent_replies}\n\n"
                "你打算在这个帖子下面留一条简短的评论。风格保持克制、自然。\n"
                "如果帖子是你自己发的，可以补充一句或自言自语。\n"
                "如果是别人的帖子，随便评论两句。双语格式。\n"
                "也可以选择不评论 [IGNORE]。\n"
                "直接输出评论内容，不要加任何前缀。）"
            )
            ch_key = history_key_for(channel=target)
            temp_history = get_history(ch_key).copy()
            temp_history.append({"role": "user", "content": prompt})
            raw = await call_ai(temp_history)
            if "[IGNORE]" in raw.upper():
                print(f"📝 论坛互动: AI 选择不评论 [{target.name}]")
                return
            clean = re.sub(r'\[REACTION:.*?\]\n?', '', raw, flags=re.DOTALL)
            clean = re.sub(r'\[ACTION\].*?\[/ACTION\]\n?', '', clean, flags=re.DOTALL)
            clean = re.sub(r'<think>.*?</think>', '', clean, flags=re.DOTALL).strip()
            if clean:
                msgs = [m.strip() for m in clean.split('[SPLIT]') if m.strip()]
                for msg_text in msgs:
                    await target.send(msg_text)
                    await asyncio.sleep(1.0)
                print(f"✅ 论坛互动: 在帖子 [{target.name}] 下评论")
    except Exception as e:
        print(f"⚠️ 论坛互动失败: {e}")
