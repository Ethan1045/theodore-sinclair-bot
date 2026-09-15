"""所有配置常量与密钥加载。无内部依赖，任何模块都可安全 import。"""
import os
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ==== 对话历史常量 ====
MAX_HISTORY = 16
HISTORY_TRIM_TO = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
LLM_TIMEOUT_SECONDS = 90
SYSTEM_HISTORY_KEY = "__system__"
COOLDOWN_SECONDS = 8
CARE_REMINDER_COOLDOWN_HOURS = 5

# ==== 消息合并窗口 ====
MERGE_WINDOW_SEC = float(os.getenv("MERGE_WINDOW_SEC", "4.0") or "4.0")
MERGE_MAX_BATCH = int(os.getenv("MERGE_MAX_BATCH", "6") or "6")
TYPING_GRACE_SEC = float(os.getenv("TYPING_GRACE_SEC", "6.0") or "6.0")
TYPING_MERGE_MAX_SEC = float(os.getenv("TYPING_MERGE_MAX_SEC", "15.0") or "15.0")

# ==== 历史桶闲置清理 ====
HISTORY_IDLE_DAYS = int(os.getenv("HISTORY_IDLE_DAYS", "7") or "7")
FRIEND_MAX_HISTORY = 6
FRIEND_HISTORY_IDLE_DAYS = 1

# ==== AI 限流 ====
AI_MAX_RPM = int(os.getenv("AI_MAX_RPM", "15") or "15")
DAILY_TOKEN_BUDGET = 0  # 在 _load() 里覆盖

# ==== 安静频道衰减系数 ====
QUIET_CHANNEL_FACTOR = 0.15

# ==== Presence 冷却 ====
PRESENCE_CHANGE_COOLDOWN_SEC = 10 * 60

# ==== 记忆条数上限 ====
MEMORY_LIMIT = 30
MEMORY_TARGET = 24
MEMORY_MAX_AGE_DAYS = 45
MEMORY_CATEGORIES = ("健康", "偏好", "关系", "计划", "情绪", "日期", "日常")


def his_now() -> datetime:
    """他此刻的当地时间：出差时是目的地时间，平时是伦敦。

    config 要保持「无内部依赖」，所以 trips 只在函数里局部 import；
    任何异常都回落到伦敦，绝不让时间函数把整条链路带崩。
    """
    try:
        import trips
        return trips.local_now()
    except Exception:
        return datetime.now(ZoneInfo("Europe/London"))


# ==== 睡眠模式（按他当地时区；出差时跟着目的地走）====
SLEEP_START_HOUR = 0       # 默认入睡时间（他当地时间 00:00）
SLEEP_END_HOUR = 5         # 默认醒来时间（他当地时间 05:00）
SLEEP_LATE_CHANCE = 0.30   # 熬夜概率（推迟入睡 0.5-1.5h）
SLEEP_DEEP_START = 1       # 深睡开始（他当地时间 01:00）
SLEEP_DEEP_END = 4         # 深睡结束（他当地时间 04:00）


def get_london_hour() -> int:
    """他当地时间的小时数（出差时是目的地的小时，不是伦敦的）。"""
    return his_now().hour


def get_london_minute() -> int:
    return his_now().minute


def is_sleep_time() -> tuple[bool, str]:
    """返回 (是否睡觉, 睡眠阶段: 'deep'|'light'|'awake')，按他当地时间算。"""
    now = his_now()
    h = now.hour
    if SLEEP_DEEP_START <= h < SLEEP_DEEP_END:
        return True, "deep"
    if h == SLEEP_START_HOUR or h == SLEEP_DEEP_END:
        return True, "light"
    if h == SLEEP_END_HOUR and now.minute < 30:
        return True, "light"
    return False, "awake"


def is_work_time() -> bool:
    """他当地时间的工作日 9:00-17:00"""
    now = his_now()
    if now.weekday() >= 5:
        return False
    return 9 <= now.hour < 17


def load_local_secrets() -> dict:
    path = os.path.join(os.path.dirname(__file__), "secrets.local.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"⚠️ 读取 secrets.local.json 失败：{e}")
        return {}


_secrets = load_local_secrets()


def _read_int_id(key: str) -> int:
    raw = os.getenv(key, "").strip() or str(_secrets.get(key, "")).strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        print(f"⚠️ {key} 解析失败：{raw!r}，按 0 处理")
        return 0


def _read_id_set(key: str) -> set[int]:
    raw_env = os.getenv(key, "").strip()
    raw_sec = _secrets.get(key)
    out: set[int] = set()
    candidates: list = []
    if raw_env:
        candidates.append(raw_env)
    if isinstance(raw_sec, list):
        candidates.extend(raw_sec)
    elif raw_sec is not None:
        candidates.append(raw_sec)
    for c in candidates:
        if isinstance(c, int):
            out.add(c)
            continue
        for part in str(c).replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                out.add(int(part))
    return out


# ==== 密钥 ====
DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN", "").strip() or str(_secrets.get("DISCORD_TOKEN", "")).strip())
API_KEY = (
    os.getenv("OPENAI_API_KEY", "").strip()
    or os.getenv("API_KEY", "").strip()
    or str(_secrets.get("OPENAI_API_KEY", "")).strip()
    or str(_secrets.get("API_KEY", "")).strip()
)
BASE_URL = (
    os.getenv("OPENAI_BASE_URL", "").strip()
    or os.getenv("BASE_URL", "").strip()
    or str(_secrets.get("OPENAI_BASE_URL", "")).strip()
    or str(_secrets.get("BASE_URL", "")).strip()
    or "https://api.openai.com/v1/"
)
MODEL_NAME = (
    os.getenv("MODEL_NAME", "").strip()
    or str(_secrets.get("MODEL_NAME", "")).strip()
    or "gpt-4o"
)
DATABASE_URL = (
    os.getenv("DATABASE_URL", "").strip()
    or str(_secrets.get("DATABASE_URL", "")).strip()
)

DAILY_TOKEN_BUDGET = int(os.getenv("DAILY_TOKEN_BUDGET", str(_secrets.get("DAILY_TOKEN_BUDGET", "0"))) or "0")

# ==== 频道 ID ====
PROACTIVE_CHANNEL_ID = int(os.getenv("PROACTIVE_CHANNEL_ID", str(_secrets.get("PROACTIVE_CHANNEL_ID", "0"))) or "0")
PARTNER_HOME_CHANNEL_ID = int(os.getenv("PARTNER_HOME_CHANNEL_ID", str(_secrets.get("PARTNER_HOME_CHANNEL_ID", "0"))) or "0")
QUIET_CHANNEL_IDS: set[int] = _read_id_set("QUIET_CHANNEL_IDS")
SILENT_CHANNEL_IDS: set[int] = _read_id_set("SILENT_CHANNEL_IDS")
# 为空时仍只允许触发消息所在 guild；填写后进一步限制目标频道。
MUTATING_CHANNEL_IDS: set[int] = _read_id_set("MUTATING_CHANNEL_IDS")

# ==== 用户 ID ====
PARTNER_USER_ID = _read_int_id("PARTNER_USER_ID")
PARTNER_FRIEND_IDS: set[int] = _read_id_set("PARTNER_FRIEND_IDS")
DM_WHITELIST_IDS = {PARTNER_USER_ID} | PARTNER_FRIEND_IDS

# ==== 启动检查 ====
if not DISCORD_TOKEN:
    raise RuntimeError("缺少 DISCORD_TOKEN（环境变量或 secrets.local.json）")
if not API_KEY:
    raise RuntimeError("缺少 OPENAI_API_KEY/API_KEY（环境变量或 secrets.local.json）")
if not PARTNER_USER_ID:
    raise RuntimeError("缺少 PARTNER_USER_ID 配置（请在 secrets.local.json 或环境变量里设置）")

# ==== 重要日期 ====
IMPORTANT_DATES: list[dict] = [
    {"month": 1,  "day": 1,  "label": "新年",        "enabled": True},
    {"month": 2,  "day": 14, "label": "情人节",      "enabled": True},
    {"month": 3,  "day": 14, "label": "白色情人节",  "enabled": True},
    {"month": 4,  "day": 1,  "label": "愚人节",      "enabled": True},
    {"month": 4,  "day": 5,  "label": "清明",        "enabled": True},
    {"month": 4,  "day": 23, "label": "读书日",      "enabled": True},
    {"month": 5,  "day": 1,  "label": "劳动节",      "enabled": True},
    {"month": 6,  "day": 1,  "label": "儿童节",      "enabled": True},
    {"month": 10, "day": 31, "label": "万圣节",      "enabled": True},
    {"month": 12, "day": 24, "label": "平安夜",      "enabled": True},
    {"month": 12, "day": 25, "label": "圣诞节",      "enabled": True},
    {"month": 12, "day": 31, "label": "跨年夜",      "enabled": True},
]


def get_today_occasion() -> str | None:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    for entry in IMPORTANT_DATES:
        if not entry.get("enabled", False):
            continue
        if entry["month"] == 0 or entry["day"] == 0:
            continue
        if entry["month"] == now.month and entry["day"] == now.day:
            return entry["label"]
    return None


# ==== 时间/表情工具（无 discord 依赖，放这里更轻量）====
def get_time_context_note() -> str:
    """两个时钟：他自己的当地时间（出差时跟着目的地走），和恋人的北京时间。

    只给一个时间会出事：出差时提示词里写着「你的当地时间是东京时间」，
    却从没告诉他东京几点，模型只能拿北京时间硬凑。
    """
    now_local = his_now()
    now_her = datetime.now(ZoneInfo("Asia/Shanghai"))
    weekdays_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    city_cn, is_trip = "伦敦", False
    try:
        import trips
        location = trips.current_location()
        city_cn, is_trip = location["city_cn"], location["is_trip"]
    except Exception:
        pass
    tz_label = now_local.tzname() or ""
    offset_hours = (
        (now_her.utcoffset() or timedelta()).total_seconds()
        - (now_local.utcoffset() or timedelta()).total_seconds()
    ) / 3600
    if abs(offset_hours) < 0.01:
        gap_note = "你们此刻没有时差，同一个时间。"
    elif offset_hours > 0:
        gap_note = f"她那边比你早 {offset_hours:g} 小时。"
    else:
        gap_note = f"她那边比你晚 {abs(offset_hours):g} 小时。"
    where = (
        f"你正在{city_cn}出差，所以你的当地时间是{city_cn}时间，不是伦敦时间"
        if is_trip else "你本人生活在伦敦"
    )
    return (
        f"（系统时间：{where}。以下两个时间分工明确，绝对不要互相混用：\n"
        f"① 你的当地时间（{city_cn}）：{now_local.strftime('%Y-%m-%d')} "
        f"{weekdays_cn[now_local.weekday()]} {now_local.strftime('%H:%M')} {tz_label}。"
        "你自己在做什么、几点了、今天还是明天、你该不该睡、你的日程与状态栏，全部以它为准。\n"
        f"② 恋人的时间（北京时间，UTC+8）：{now_her.strftime('%Y-%m-%d')} "
        f"{weekdays_cn[now_her.weekday()]} {now_her.strftime('%H:%M')}。"
        "判断她那边是早上还是深夜、她是不是该吃饭该睡了、问她今天过得怎么样、"
        "说早安晚安，一律以它为准。\n"
        f"{gap_note}）"
    )


def get_beijing_time_note() -> str:
    """旧调用兼容层；返回内容已改为「他的当地时间 + 恋人的北京时间」双时区上下文。"""
    return get_time_context_note()


def get_presence_time_context() -> str:
    """他的当地时间 + 恋人的北京时间。出差时前者跟着目的地走，后者永远是北京。"""
    now_local = his_now()
    now_beijing = datetime.now(ZoneInfo("Asia/Shanghai"))
    weekday_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][now_local.weekday()]
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now_beijing.weekday()]
    city_en = "London"
    try:
        import trips
        city_en = trips.current_location()["city_en"]
    except Exception:
        pass
    return (
        f"(Your local time — {city_en}: {now_local.strftime('%Y-%m-%d')} {weekday_en} {now_local.strftime('%H:%M')}. "
        f"Your partner's time — Beijing: {now_beijing.strftime('%Y-%m-%d')} {weekday_cn} {now_beijing.strftime('%H:%M')}.)"
    )
