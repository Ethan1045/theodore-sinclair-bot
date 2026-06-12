"""AI 客户端、RPM 限流闸门、token 预算追踪、call_ai。"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

import config
import state


ai_client = AsyncOpenAI(api_key=config.API_KEY, base_url=config.BASE_URL, timeout=config.LLM_TIMEOUT_SECONDS)
_raw_completions = ai_client.chat.completions


class _AsyncRpmGate:
    def __init__(self, max_per_min: int):
        self.max_per_min = max_per_min
        self._hits: list[float] = []
        self._lock: "asyncio.Lock | None" = None

    async def acquire(self) -> None:
        if self.max_per_min <= 0:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            while True:
                now = asyncio.get_running_loop().time()
                self._hits = [t for t in self._hits if now - t < 60.0]
                if len(self._hits) < self.max_per_min:
                    self._hits.append(now)
                    return
                wait = 60.0 - (now - self._hits[0]) + 0.05
                print(f"⏳ AI 请求达到每分钟上限({self.max_per_min})，排队等待 {wait:.1f}s")
                await asyncio.sleep(wait)


_ai_rpm_gate = _AsyncRpmGate(config.AI_MAX_RPM)


async def ai_chat_create(**kwargs):
    await _ai_rpm_gate.acquire()
    resp = await _raw_completions.create(**kwargs)
    # 所有 AI 调用都从这里走，集中记账，确保辅助调用（摘要/presence/记忆/卡片）
    # 同样计入每日 token 预算，避免账单保护被绕过。
    try:
        record_token_usage(getattr(resp, "usage", None))
    except Exception:
        pass
    return resp


# ==== Token 用量追踪 ====
def _today_key_bj() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def _roll_token_usage_if_new_day() -> None:
    today = _today_key_bj()
    if state._token_usage.get("date") != today:
        if state._token_usage.get("date") is not None:
            print(
                f"📊 昨日 token 用量 [{state._token_usage['date']}]: "
                f"calls={state._token_usage['calls']} prompt={state._token_usage['prompt']} "
                f"completion={state._token_usage['completion']} total={state._token_usage['total']} "
                f"throttled={state._token_usage['throttled']}"
            )
        state._token_usage.update({"date": today, "prompt": 0, "completion": 0, "total": 0, "calls": 0, "throttled": 0})


def record_token_usage(usage_obj) -> None:
    _roll_token_usage_if_new_day()
    state._token_usage["calls"] += 1
    if usage_obj is None:
        return
    try:
        p = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        c = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        t = int(getattr(usage_obj, "total_tokens", p + c) or (p + c))
    except Exception:
        return
    state._token_usage["prompt"] += p
    state._token_usage["completion"] += c
    state._token_usage["total"] += t


def is_over_daily_budget() -> bool:
    if config.DAILY_TOKEN_BUDGET <= 0:
        return False
    _roll_token_usage_if_new_day()
    return state._token_usage["total"] >= config.DAILY_TOKEN_BUDGET


async def call_ai(history: list) -> str:
    if is_over_daily_budget():
        state._token_usage["throttled"] += 1
        raise RuntimeError(
            f"⛔ 已达每日 token 预算上限 ({config.DAILY_TOKEN_BUDGET})，拒绝本次调用以保护账单。"
        )

    rate_limit_delays = (12,)
    transient_delays = (2, 4, 8)
    rate_attempt = 0
    transient_attempt = 0
    last_exc: Exception | None = None
    while True:
        try:
            response = await ai_chat_create(
                model=config.MODEL_NAME,
                messages=history,
            )
            content = response.choices[0].message.content if response.choices else ""
            return content or ""
        except Exception as e:
            last_exc = e
            err_str = str(e).lower()
            is_rate_limit = (
                "429" in str(e)
                or "rate limit" in err_str
                or "too many requests" in err_str
                or "quota" in err_str
                or "overloaded" in err_str
            )
            is_transient = (
                "timeout" in err_str
                or "timed out" in err_str
                or "connection" in err_str
                or isinstance(e, (asyncio.TimeoutError,))
            )
            if not (is_rate_limit or is_transient):
                raise
            state._token_usage["throttled"] += 1
            if is_rate_limit:
                if rate_attempt >= len(rate_limit_delays):
                    print(f"⚠️ API 限流，温和重试后仍失败，本轮放弃 ({type(e).__name__}): {e}")
                    raise
                wait = rate_limit_delays[rate_attempt]
                rate_attempt += 1
                print(f"⚠️ API 限流 ({type(e).__name__})，{wait}s 后只再试一次")
            else:
                if transient_attempt >= len(transient_delays):
                    print(f"⚠️ API 瞬时错误多次重试仍失败 ({type(e).__name__}): {e}")
                    raise
                wait = transient_delays[transient_attempt]
                transient_attempt += 1
                print(f"⚠️ API 瞬时错误 ({type(e).__name__})，{wait}s 后重试 ({transient_attempt}/{len(transient_delays)})")
            await asyncio.sleep(wait)
    if last_exc:
        raise last_exc
    return ""
