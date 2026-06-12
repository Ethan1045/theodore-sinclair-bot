"""所有配置常量与密钥加载。无内部依赖，任何模块都可安全 import。"""
import os
import json
from datetime import datetime
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

# ==== 历史桶闲置清理 ====
HISTORY_IDLE_DAYS = int(os.getenv("HISTORY_IDLE_DAYS", "7") or "7")

# ==== AI 限流 ====
AI_MAX_RPM = int(os.getenv("AI_MAX_RPM", "15") or "15")
DAILY_TOKEN_BUDGET = 0  # 在下面覆盖

# ==== 安静频道衰减系数 ====
QUIET_CHANNEL_FACTOR = 0.15

# ==== Presence 冷却 ====
PRESENCE_CHANGE_COOLDOWN_SEC = 10 * 60

# ==== 记忆条数上限 ====
MEMORY_LIMIT = 30
MEMORY_TARGET = 24
MEMORY_CATEGORIES = ("健康", "偏好", "关系", "计划", "情绪", "日期", "日常")


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
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

DAILY_TOKEN_BUDGET = int(os.getenv("DAILY_TOKEN_BUDGET", str(_secrets.get("DAILY_TOKEN_BUDGET", "0"))) or "0")

# ==== 频道 ID ====
PROACTIVE_CHANNEL_ID = int(os.getenv("PROACTIVE_CHANNEL_ID", str(_secrets.get("PROACTIVE_CHANNEL_ID", "0"))) or "0")
PARTNER_HOME_CHANNEL_ID = int(os.getenv("PARTNER_HOME_CHANNEL_ID", str(_secrets.get("PARTNER_HOME_CHANNEL_ID", "0"))) or "0")
QUIET_CHANNEL_IDS: set[int] = _read_id_set("QUIET_CHANNEL_IDS")
SILENT_CHANNEL_IDS: set[int] = _read_id_set("SILENT_CHANNEL_IDS")

# ==== 用户 ID ====
PARTNER_USER_ID = _read_int_id("PARTNER_USER_ID")
DM_WHITELIST_IDS = {PARTNER_USER_ID} if PARTNER_USER_ID else set()

# ==== 启动检查 ====
if not DISCORD_TOKEN:
    raise RuntimeError("缺少 DISCORD_TOKEN（请在 secrets.local.json 或环境变量里设置）")
if not API_KEY:
    raise RuntimeError("缺少 OPENAI_API_KEY/API_KEY（请在 secrets.local.json 或环境变量里设置）")
if not PARTNER_USER_ID:
    print(
        "⚠️ 警告：PARTNER_USER_ID 未配置。T.S. 仍能跑，但他将无法识别你为「她」，"
        "也不会触发恋人专属的回复概率、主动私信、记忆系统、上线感知等。"
        "强烈建议在 secrets.local.json 或环境变量里填上你自己的 Discord 用户 ID。"
    )

# ==== 重要日期（玩家可自由增减） ====
# 想加自己的生日/纪念日，在下面 append 一条即可：
#   {"month": 12, "day": 10, "label": "她的生日", "enabled": True}
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
def get_beijing_time_note() -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    return f"（系统时间：北京时间现在是 {now.strftime('%Y-%m-%d')} {weekday_cn} {now.strftime('%H:%M')}。）"
