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
    (("毕设", "实验", "化学", "论文"),               "watching",  "Thinking of her lab notes"),
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
    # The persistent day plan is the source of truth; ad-hoc keyword changes
    # would make the avatar disagree with reply/proactive context.
    if state.current_life_slot:
        return
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
    if state.current_life_slot:
        return
    if random.random() > 0.35:
        return
    if not _presence_cooldown_ok():
        return
    lowered = text.lower()
    for keywords, type_str, status_text in KEYWORD_STATUS_MAP:
        if any(k in lowered for k in keywords):
            await apply_presence(type_str, status_text, source="keyword")
            return


async def generate_presence() -> tuple[str, discord.ActivityType, str]:
    from config import get_presence_time_context
    time_ctx = get_presence_time_context()
    prompt = (
        f"{time_ctx}\n"
        "Generate a Discord status for T.S. (Theodore Sinclair / 沈玘言), a 32-year-old Anglo-Chinese man living in London.\n"
        "The status appears next to his avatar as 'Listening to xxx' / 'Playing xxx' / 'Watching xxx'.\n\n"
        "【Rules】\n"
        "1. Base the activity on HIS London local time — what would he plausibly be doing right now?\n"
        "1a. His temperament is consistently gentle, composed, restrained, highly capable and well-mannered. The status must fit his established life: family-office work, foundation governance, archival/book conservation, serious reading, fencing, swimming, riding, tea, restrained music listening or quiet travel.\n"
        "1b. Do not invent quirky habits, comic incompetence, flippant thoughts, internet slang, attention-seeking moods, melodrama or random contrast for 'human realism'. Never make him look unserious or out of character.\n"
        "2. listening → a real song / album / artist\n"
        "3. playing → a specific thing he's doing\n"
        "4. watching → something he's observing or paying attention to\n"
        "5. Vary the type — don't always pick the same category\n"
        "6. Status text: max 25 characters, no quotes, no explanation\n"
        "7. Language: mostly English (about 70-80%), occasionally Chinese or mixed\n"
        "8. Duration: classify the status as 'instant' or 'sustained':\n"
        "   - instant: brief/momentary actions (adjusting cufflinks, checking the time, flipping a coin, glancing out the window)\n"
        "   - sustained: ongoing activities (reading, listening to music, working on documents, fencing practice)\n\n"
        "Output format (strictly one line): TYPE|DURATION|TEXT\n"
        "TYPE = listening / playing / watching\n"
        "DURATION = instant / sustained\n"
        "Example: playing|instant|Adjusting cufflinks\n"
        "Example: listening|sustained|Chet Baker - Almost Blue\n"
        "Now output:"
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
        parts = raw.split("|")
        if len(parts) >= 3:
            type_str = parts[0].strip().lower()
            duration_type = parts[1].strip().lower()
            text = "|".join(parts[2:]).strip()[:128]
        else:
            type_str = parts[0].strip().lower()
            duration_type = "sustained"
            text = parts[1].strip()[:128]

        if duration_type not in ("instant", "sustained"):
            duration_type = "sustained"

        if text in state._recent_presences:
            fallback_texts = [
                "Restoring a 19th c. spine", "Reviewing family-office papers",
                "Window light on old paper", "Late letters to Geneva",
                "Chet Baker in the study", "Re-shelving first editions",
                "Foundation papers", "Watching London fog collect",
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
        print(f"🎭 AI生成状态: [{type_str}|{duration_type}] {text}")
        return text, activity_type, duration_type
    except Exception as e:
        print(f"AI生成状态失败，使用fallback: {e}")
        fallbacks = [
            ("Manuscript restoration", discord.ActivityType.playing, "sustained"),
            ("Chet Baker - Almost Blue", discord.ActivityType.listening, "sustained"),
            ("Reviewing filings", discord.ActivityType.playing, "sustained"),
            ("Dusting the archive room", discord.ActivityType.playing, "sustained"),
        ]
        text, activity_type, dur = random.choice(fallbacks)
        if text not in state._recent_presences:
            state._recent_presences.append(text)
            if len(state._recent_presences) > 10:
                state._recent_presences = state._recent_presences[-10:]
        return text, activity_type, dur


async def generate_custom_bubble() -> str:
    from config import get_presence_time_context
    time_ctx = get_presence_time_context()
    recent_str = ", ".join(state._recent_presences[-5:]) if state._recent_presences else "none"

    prompt = (
        f"{time_ctx}\n"
        "Generate a Discord custom status bubble for T.S. (Theodore Sinclair / 沈玘言), "
        "a 32-year-old Anglo-Chinese man living in London.\n"
        "This text appears in a bubble next to his avatar, like a real person's casual status update.\n\n"
        "【Direction (pick one randomly)】\n"
        "- A random thought in his head (very short, like talking to himself)\n"
        "- Weather + feeling about it\n"
        "- A small thing he's doing right now\n"
        "- A cryptic one-liner that fits his vibe\n\n"
        "【Language】Mostly English (70-80%), occasionally Chinese or mixed. He's half-British half-Chinese.\n"
        "【Time】Base it on his London local time, not Beijing time.\n"
        "【Forbidden】Motivational quotes, saccharine tone, marketing-speak\n"
        "- Don't repeat recent statuses: " + recent_str + "\n\n"
        "【Format】Max 40 characters, shorter is better, output exactly one line.\n\n"
        "Now output:"
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
            temperature=0.9,
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


def get_guild_sticker_hint(guild: "discord.Guild | None") -> str:
    """Expose only stickers the bot can actually send in the current guild."""
    if not guild:
        return ""
    stickers = [s for s in guild.stickers if getattr(s, "available", True)]
    if not stickers:
        return ""
    lines = [f"- {s.name}（sticker_id={s.id}）" for s in stickers[:20]]
    return (
        "\n\n【本服务器贴纸】极少数时候，你可以不发正文、只用一个服务器贴纸回应。"
        "仅可从下列清单选择，并输出 SEND_STICKER 动作；不要臆造ID，也不要与文字表情同时滥用。\n"
        + "\n".join(lines)
    )
