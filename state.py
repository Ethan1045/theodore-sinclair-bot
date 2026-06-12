"""可变全局状态、锁基础设施、后台任务追踪。

所有需要在多个模块间共享并且会被重新赋值的变量都放在这里。
调用方应 `import state`，通过 `state.xxx` 访问或修改，
而不是 `from state import xxx`（避免 rebinding 失效问题）。
不可变常量请从 config.py 导入。
"""
import asyncio
import traceback as _tb
from datetime import datetime, timezone

# ==== 强制指令 ====
mandatory_instruction = None

# ==== 防刷屏冷却 ====
user_cooldowns: dict[int, datetime] = {}

# ==== 恋人最近活动时间 ====
last_partner_activity_at: datetime | None = None

# ==== Discord presence 状态 ====
current_presence: dict | None = None
last_presence_change_at: datetime | None = None


def set_current_presence(kind: str, text: str, *, source: str = "auto"):
    global current_presence, last_presence_change_at
    current_presence = {
        "kind": kind,
        "text": text,
        "since": datetime.now(timezone.utc),
        "source": source,
    }
    last_presence_change_at = current_presence["since"]


def presence_hint_text() -> str:
    if not current_presence:
        return ""
    kind = current_presence.get("kind") or ""
    text = (current_presence.get("text") or "").strip()
    if not text:
        return ""
    since = current_presence.get("since")
    mins = 0
    if isinstance(since, datetime):
        mins = max(0, int((datetime.now(timezone.utc) - since).total_seconds() // 60))
    label_map = {
        "listening": "正在听",
        "playing":   "正在做",
        "watching":  "正在看",
        "custom":    "现在的状态",
    }
    label = label_map.get(kind, "现在的状态")
    duration = f"，已经持续约 {mins} 分钟" if mins >= 5 else ""
    return (
        f"\n（系统背景：你的头像状态此刻显示「{label}：{text}」{duration}。"
        "若话题自然契合，可以顺手带出，但不要硬提；如果聊到了相关音乐/书/活动，"
        "可以在消息末尾用 [LINK:...] 给一个链接。）"
    )


# ==== 提醒系统 ====
pending_reminders: list[dict] = []
reminders_lock: "asyncio.Lock | None" = None

# ==== 吃饭/睡觉提醒冷却 ====
care_reminder_last: dict[str, datetime] = {}

# ==== 恋人上线感知 ====
partner_last_seen_online: datetime | None = None

# ==== 节日发言防重复 ====
_last_occasion_date: str = ""

# ==== 频道活跃度追踪 ====
channel_activity: dict[int, list] = {}
ACTIVITY_WINDOW_SECS = 25
ACTIVITY_HOT_THRESHOLD = 4

# ==== 最近使用过的 presence，避免重复 ====
_recent_presences: list[str] = []

# ==== 对话历史 ====
_histories: dict[str, list[dict]] = {}
_dirty_history_buckets: set[str] = set()
_bucket_locks: dict[str, asyncio.Lock] = {}
_bucket_touched: dict[str, datetime] = {}
_trimming: set[str] = set()

history_lock: "asyncio.Lock | None" = None
_merge_lock: "asyncio.Lock | None" = None
_bg_lock: "asyncio.Lock | None" = None

# ==== 消息合并窗口状态 ====
_merge_state: dict[tuple, dict] = {}

# ==== 后台任务追踪 ====
_bg_tasks: set[asyncio.Task] = set()

# ==== Token 用量 ====
_token_usage: dict = {"date": None, "prompt": 0, "completion": 0, "total": 0, "calls": 0, "throttled": 0}


def _ensure_locks() -> None:
    global history_lock, reminders_lock, _merge_lock, _bg_lock
    if history_lock is None:
        history_lock = asyncio.Lock()
    if reminders_lock is None:
        reminders_lock = asyncio.Lock()
    if _merge_lock is None:
        _merge_lock = asyncio.Lock()
    if _bg_lock is None:
        _bg_lock = asyncio.Lock()


def get_bucket_lock(key: str) -> asyncio.Lock:
    lk = _bucket_locks.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _bucket_locks[key] = lk
    return lk


def touch_bucket(key: str) -> None:
    _bucket_touched[key] = datetime.now(timezone.utc)


def mark_history_dirty(key: str) -> None:
    from config import SYSTEM_HISTORY_KEY
    if key and key != SYSTEM_HISTORY_KEY:
        _dirty_history_buckets.add(key)


def spawn_bg(coro, *, name: str = "bg"):
    task = asyncio.create_task(coro, name=name)
    _bg_tasks.add(task)
    def _done(t: asyncio.Task):
        _bg_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            print(f"⚠️ 后台任务 {t.get_name()} 异常: {type(exc).__name__}: {exc}")
            _tb.print_exception(type(exc), exc, exc.__traceback__)
    task.add_done_callback(_done)
    return task
