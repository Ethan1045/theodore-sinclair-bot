"""Discord presence/状态栏管理：关键词彩蛋、AI生成状态、显式活动同步。"""
import asyncio
import random
import re
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord

import config
import state
from client import discord_client
from ai_client import ai_chat_create

# ==== 关键词状态彩蛋 ====
KEYWORD_STATUS_MAP: list[tuple] = [
    (("击剑", "剑", "fencing", "julian"),           "playing",   "Sparring. Don't interrupt."),
    (("怀表", "pocket watch", "上弦"),               "playing",   "Winding the pocket watch"),
    (("调香", "香水", "bergamot", "檀木", "龙涎"),  "playing",   "Blending a new scent"),
    (("黑胶", "唱片", "vinyl"),                      "listening", "Vinyl. Lights off."),
    (("游泳", "泳池", "潜水"),                       "playing",   "Late swim"),
    (("骑马", "马术", "equestrian"),                 "playing",   "Equestrian"),
    (("档案", "古籍", "修复", "装帧"),               "playing",   "Archive restoration"),
    (("威士忌", "whisky", "单一麦芽"),               "watching",  "Single malt, alone"),
    (("伦敦", "london", "雾", "fog"),                "watching",  "London in the rain"),
    (("咖啡", "手冲", "coffee"),                     "playing",   "Third cup"),
    (("茶", "普洱", "岩茶", "伯爵"),                 "playing",   "Earl grey, going cold"),
    (("书", "读书", "阅读", "看书", "图书馆"),       "playing",   "Reading. Do not disturb."),
    (("猎鹰", "falcon"),                             "playing",   "Falconry grounds"),
    (("日内瓦", "巴黎", "出差", "飞机", "机场"),     "watching",  "Somewhere over Europe"),
]

_TYPE_MAP = {
    "listening": discord.ActivityType.listening,
    "playing":   discord.ActivityType.playing,
    "watching":  discord.ActivityType.watching,
}

_EXPLICIT_ACTIVITY_RE = re.compile(
    r"(?:我|i\s*am|i'm)\s*(?:正在|在|now\s*)?(?P<verb>听|在听|读|看|玩|listening to|reading|watching|playing)\s+(?P<obj>[^\n,。！？!?…]{2,40})",
    re.IGNORECASE,
)
_VERB_TO_KIND = {
    "听": "listening", "在听": "listening", "listening to": "listening",
    "读": "playing", "reading": "playing", "看": "watching", "watching": "watching",
    "玩": "playing", "playing": "playing",
}


def _presence_cooldown_ok() -> bool:
    if state.last_presence_change_at is None:
        return True
    return (datetime.now(timezone.utc) - state.last_presence_change_at).total_seconds() >= config.PRESENCE_CHANGE_COOLDOWN_SEC


async def apply_presence(kind: str, text: str, *, source: str) -> bool:
    if kind not in _TYPE_MAP:
        return False
    if not _presence_cooldown_ok():
        return False
    try:
        await discord_client.change_presence(
            status=discord.Status.idle,
            activity=discord.Activity(type=_TYPE_MAP[kind], name=text),
        )
        state.set_current_presence(kind, text, source=source)
        print(f"🎭 presence → [{kind}] {text}  (source={source})")
        return True
    except Exception as e:
        print(f"⚠️ presence 切换失败: {e}")
        return False


async def try_explicit_activity_sync(text: str) -> None:
    if not text or not _presence_cooldown_ok():
        return
    m = _EXPLICIT_ACTIVITY_RE.search(text)
    if not m:
        return
    kind = _VERB_TO_KIND.get(m.group("verb").lower())
    obj = m.group("obj").strip(" 。！？!?，,…\"'""「」")
    if not kind or not obj or len(obj) < 2:
        return
    label_map = {"listening": "Listening alongside her", "watching": "Watching with her", "playing": "With her"}
    text_out = f"{label_map.get(kind, 'With her')}: {obj}"
    await apply_presence(kind, text_out, source="partner-mirror")


async def try_keyword_presence_update(text: str) -> None:
    if random.random() > 0.35:
        return
    if not _presence_cooldown_ok():
        return
    lowered = text.lower()
    for keywords, type_str, status_text in KEYWORD_STATUS_MAP:
        if any(k in lowered for k in keywords):
            await apply_presence(type_str, status_text, source="keyword")
            return


async def generate_presence() -> tuple[str, discord.ActivityType]:
    from config import get_beijing_time_note
    time_ctx = get_beijing_time_note()
    prompt = (
        f"{time_ctx}\n"
        "你需要为T.S.（Theodore Sinclair / 沈玘言）生成一条Discord在线状态。\n"
        "状态格式为『正在听 xxx』『正在玩 xxx』『正在看 xxx』，显示在头像旁边。\n\n"
        "【规则】\n"
        "1. 根据当前时间推断他可能在做什么，自由发挥，不要总重复同一类型\n"
        "2. listening → 一首真实歌曲/专辑/艺术家\n"
        "3. playing → 他正在做的一件具体的事\n"
        "4. watching → 他在注视/关注某件事\n"
        "5. 鼓励生成意想不到但仍符合人设的组合，不要每次都选同一类型\n"
        "6. 状态文字最多20个字符，不加引号，不解释\n\n"
        "输出格式（严格只输出一行）：TYPE|TEXT\n"
        "TYPE 是 listening / playing / watching 之一。\n"
        "现在输出："
    )
    try:
        response = await ai_chat_create(
            model=config.MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0.9,
        )
        raw = response.choices[0].message.content
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        raw = raw.splitlines()[0].strip()
        if "|" not in raw:
            raise ValueError(f"格式错误: {raw}")
        type_str, text = raw.split("|", 1)
        type_str = type_str.strip().lower()
        text = text.strip()[:128]

        if text in state._recent_presences:
            fallback_texts = [
                "Restoring a 19th c. spine", "Quietly bullying spreadsheets",
                "Window light on old paper", "Late letters to Geneva",
                "Listening for your typing", "Re-shelving first editions",
                "Adjusting cufflinks again", "Watching London fog collect",
            ]
            unused = [t for t in fallback_texts if t not in state._recent_presences]
            text = random.choice(unused or fallback_texts)

        state._recent_presences.append(text)
        if len(state._recent_presences) > 10:
            state._recent_presences = state._recent_presences[-10:]

        type_map = {
            "listening": discord.ActivityType.listening,
            "playing": discord.ActivityType.playing,
            "watching": discord.ActivityType.watching,
        }
        activity_type = type_map.get(type_str, discord.ActivityType.playing)
        print(f"🎭 AI生成状态: [{type_str}] {text}")
        return text, activity_type
    except Exception as e:
        print(f"AI生成状态失败，使用fallback: {e}")
        fallbacks = [
            ("Manuscript restoration", discord.ActivityType.playing),
            ("Chet Baker - Almost Blue", discord.ActivityType.listening),
            ("Reviewing filings", discord.ActivityType.playing),
            ("Dusting the archive room", discord.ActivityType.playing),
        ]
        text, activity_type = random.choice(fallbacks)
        if text not in state._recent_presences:
            state._recent_presences.append(text)
            if len(state._recent_presences) > 10:
                state._recent_presences = state._recent_presences[-10:]
        return text, activity_type


async def generate_custom_bubble() -> str:
    from config import get_beijing_time_note
    time_ctx = get_beijing_time_note()
    recent_str = "、".join(state._recent_presences[-5:]) if state._recent_presences else "无"

    prompt = (
        f"{time_ctx}\n"
        "你需要为T.S.（Theodore Sinclair / 沈玘言）生成一条Discord自定义状态气泡文字。\n"
        "这条文字会显示在他头像旁边的气泡里，像一个真实的人随手更新的状态。\n\n"
        "【内容方向（随机选一个）】\n"
        "- 脑子里的随机碎碎念（很短，像自言自语）\n"
        "- 当前天气 + 感受\n"
        "- 当下做的具体小事\n"
        "- 莫名其妙的一句话，但符合他的气质\n\n"
        "【禁止】励志体、鸡汤体、营销感\n"
        "- 重复最近用过的内容：" + recent_str + "\n\n"
        "【格式】严格不超过40个字符，越短越好，只输出一行状态文字\n\n"
        "现在输出："
    )

    fallbacks = [
        "fog again", "ran out of bergamot", "third cup", "quiet morning",
        "london in november", "tea gone cold", "she's probably asleep",
        "archive dust", "rewinding the same piece", "last of the earl grey",
        "window light", "just the clock", "再倒一杯", "又是这首", "伦敦又下雨了",
    ]

    try:
        response = await ai_chat_create(
            model=config.MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=1.3,
        )
        raw = response.choices[0].message.content.strip().splitlines()[0]
        raw = raw.strip('"\'「」“”‘’')
        text = raw[:64]

        if text in state._recent_presences:
            unused = [f for f in fallbacks if f not in state._recent_presences]
            text = random.choice(unused or fallbacks)

        state._recent_presences.append(text)
        if len(state._recent_presences) > 12:
            state._recent_presences = state._recent_presences[-12:]

        print(f"💬 AI生成气泡状态: {text}")
        return text

    except Exception as e:
        print(f"⚠️ 气泡状态生成失败，使用fallback: {e}")
        unused = [f for f in fallbacks if f not in state._recent_presences]
        text = random.choice(unused or fallbacks)
        state._recent_presences.append(text)
        if len(state._recent_presences) > 12:
            state._recent_presences = state._recent_presences[-12:]
        return text


async def get_london_weather() -> str | None:
    def _fetch():
        url = "https://wttr.in/London?format=%C+%t&lang=en"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode().strip()
    try:
        return await asyncio.to_thread(_fetch)
    except Exception:
        return None


def get_guild_emoji_hint(guild: "discord.Guild | None") -> str:
    if not guild or not guild.emojis:
        return ""
    lines = []
    for emoji in guild.emojis:
        if not emoji.available:
            continue
        tag = f"<a:{emoji.name}:{emoji.id}>" if emoji.animated else f"<:{emoji.name}:{emoji.id}>"
        lines.append(f"  {tag}（名称：{emoji.name}）")
    if not lines:
        return ""
    return (
        "\n\n【本服务器的自定义表情】你可以在聊天文本里直接使用下列自定义表情（复制粘贴整个尖括号标签即可），"
        "它们会在Discord里正确渲染成表情图片。不要滥用，只在真正合适时用一个。\n"
        + "\n".join(lines)
    )
