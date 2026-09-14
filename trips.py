"""随机出差系统。

他平时在伦敦。偶尔会出差几天——出差期间他的当地时区、作息、Discord 状态，
以及说「今天/今晚/该睡了」时的判断，全部跟着目的地走。
无论他在哪里，恋人始终在北京时间，这条不变。

本模块不在顶层 import 任何项目内模块，因此可以被 config、presence、
life_state、tasks_bg、slash_cmds 安全 import；需要落库或刷新日程时用函数内的
局部 import，避免循环依赖。

目的地和事由都是 Theodore 这个角色的公共设定（家族办公室、文化基金会、
旧书与装帧行业、各地的家族分支），不含任何部署者的私人资料；
想换成自己的城市，直接改 TRIP_DESTINATIONS 即可。
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HOME_TZ = "Europe/London"
HOME_CITY_CN = "伦敦"
HOME_CITY_EN = "London"

# 每个目的地都对应他真实会去做的事：家族办公室、文化基金会、家族分支与旧书行业。
TRIP_DESTINATIONS: list[dict] = [
    {
        "code": "new_york", "city_cn": "纽约", "city_en": "New York", "tz": "America/New_York",
        "purposes": ["和北美分支复核几笔投资", "去见家族办公室的北美对手方", "处理北美那边的治理会议"],
        "day": ("Meetings, Midtown", "在中城连着开会"),
        "night": ("New York, late", "在纽约的深夜翻文件"),
    },
    {
        "code": "boston", "city_cn": "波士顿", "city_en": "Boston", "tz": "America/New_York",
        "purposes": ["去看文保与教育方向的几个项目", "和一所大学谈装帧学徒资助"],
        "day": ("Conservation lab", "在文保实验室里待着"),
        "night": ("Boston, quiet", "在波士顿的旅馆里读东西"),
    },
    {
        "code": "zurich", "city_cn": "苏黎世", "city_en": "Zurich", "tz": "Europe/Zurich",
        "purposes": ["和受托人做家族信托的年度复核", "见私人银行那边的人"],
        "day": ("Trust review", "和受托人开会"),
        "night": ("Zurich, lakeside", "在湖边散了很久的步"),
    },
    {
        "code": "geneva", "city_cn": "日内瓦", "city_en": "Geneva", "tz": "Europe/Zurich",
        "purposes": ["主持文化基金会的理事会", "去谈一批纸张保存的合作"],
        "day": ("Foundation board", "在基金会理事会上"),
        "night": ("Geneva, evening", "在日内瓦的晚上写东西"),
    },
    {
        "code": "paris", "city_cn": "巴黎", "city_en": "Paris", "tz": "Europe/Paris",
        "purposes": ["去看一场古籍拍卖的预展", "见装帧工坊的几位老师傅", "陪法国叔叔 Charles 处理一件家族旧事"],
        "day": ("Auction preview", "在拍卖行看预展"),
        "night": ("Paris, after hours", "在巴黎的夜里走了一段"),
    },
    {
        "code": "milan", "city_cn": "米兰", "city_en": "Milan", "tz": "Europe/Rome",
        "purposes": ["去谈一批手工纸的供应", "见一家做修复材料的老厂"],
        "day": ("Paper mill visit", "在纸厂里看工序"),
        "night": ("Milan, late", "在米兰的夜里独处"),
    },
    {
        "code": "berlin", "city_cn": "柏林", "city_en": "Berlin", "tz": "Europe/Berlin",
        "purposes": ["去见德国堂姐 Elise，顺便看一个艺术史项目", "处理欧陆分支的几份文件"],
        "day": ("Archive visit", "在档案馆里翻东西"),
        "night": ("Berlin, evening", "在柏林的晚上喝了杯东西"),
    },
    {
        "code": "edinburgh", "city_cn": "爱丁堡", "city_en": "Edinburgh", "tz": "Europe/London",
        "purposes": ["去看家族在苏格兰的那处老宅", "处理历史不动产的修缮事宜"],
        "day": ("Estate walkthrough", "在老宅里逐间看过去"),
        "night": ("Edinburgh, cold", "在爱丁堡的冷夜里"),
    },
    {
        "code": "tokyo", "city_cn": "东京", "city_en": "Tokyo", "tz": "Asia/Tokyo",
        "purposes": ["去谈和纸与古籍修复的合作", "见几位做手工装帧的匠人"],
        "day": ("Washi workshop", "在和纸工坊里"),
        "night": ("Tokyo, 2 a.m.", "在东京的凌晨还没睡"),
    },
    {
        "code": "kyoto", "city_cn": "京都", "city_en": "Kyoto", "tz": "Asia/Tokyo",
        "purposes": ["去看一批需要修复的古籍", "见一位做表具的老师傅"],
        "day": ("Scroll mounting", "在看表具的工序"),
        "night": ("Kyoto, still", "在京都安静的夜里"),
    },
    {
        "code": "hong_kong", "city_cn": "香港", "city_en": "Hong Kong", "tz": "Asia/Hong_Kong",
        "purposes": ["处理亚洲这边的几笔投资", "见家族办公室的亚洲团队"],
        "day": ("Central, meetings", "在中环连轴开会"),
        "night": ("Hong Kong, humid night", "在香港潮湿的夜里"),
    },
    {
        "code": "singapore", "city_cn": "新加坡", "city_en": "Singapore", "tz": "Asia/Singapore",
        "purposes": ["去看堂弟沈言书那摊金融科技的事", "处理东南亚的架构问题"],
        "day": ("Due diligence", "在做尽调"),
        "night": ("Singapore, late", "在新加坡的夜里"),
    },
    {
        "code": "shanghai", "city_cn": "上海", "city_en": "Shanghai", "tz": "Asia/Shanghai",
        "purposes": ["去见中国叔叔沈观澜", "处理中国这边的家族事务"],
        "day": ("Family matters", "在处理家里的事"),
        "night": ("Shanghai, night", "在上海的夜里"),
    },
]

# 出差时额外可用的锚定活动：会议间隙、酒店、路上。
TRIP_ANCHOR_EXTRA: list[tuple[str, str, str]] = [
    ("playing", "Between meetings", "两场会之间"),
    ("watching", "Hotel desk", "在酒店的书桌前"),
    ("playing", "Reading on the move", "在路上读东西"),
]

# ==== 可调参数（都会随 bot_config 持久化）====
TRIP_ENABLED = True
TRIP_CHANCE_PER_DAY = 0.08     # 平均约十二天一趟
TRIP_MIN_DAYS = 2.0
TRIP_MAX_DAYS = 6.0
TRIP_MIN_GAP_DAYS = 9.0        # 两趟出差之间至少隔多久
TRIP_CHECK_INTERVAL_HOURS = 2  # 调度循环间隔，用来把日概率折算成单次概率

# code/start/end/purpose 描述当前行程；last_end 与 recent 用于控制间隔和重复。
_trip_state: dict = {
    "code": "", "start": "", "end": "", "purpose": "", "last_end": "", "recent": [],
}


def destination_by_code(code: str) -> dict | None:
    return next((d for d in TRIP_DESTINATIONS if d["code"] == (code or "")), None)


def active_trip() -> dict | None:
    """当前正在进行的出差；没有或已经过期都返回 None。

    刻意不看 TRIP_ENABLED——那个开关只管他会不会自己决定出发；
    手动派出去的行程即使关掉随机出差也照常进行，直到到期或被召回。
    """
    destination = destination_by_code(str((_trip_state or {}).get("code") or ""))
    if not destination:
        return None
    try:
        start = datetime.fromisoformat(_trip_state["start"])
        end = datetime.fromisoformat(_trip_state["end"])
    except (KeyError, TypeError, ValueError):
        return None
    now = datetime.now(timezone.utc)
    if not (start <= now < end):
        return None
    return {**destination, "start": start, "end": end, "purpose": _trip_state.get("purpose") or ""}


def current_location() -> dict:
    """他此刻人在哪里：出差目的地，否则伦敦。"""
    trip = active_trip()
    if trip:
        return {
            "city_cn": trip["city_cn"], "city_en": trip["city_en"], "tz": trip["tz"],
            "is_trip": True, "purpose": trip["purpose"], "start": trip["start"], "end": trip["end"],
        }
    return {
        "city_cn": HOME_CITY_CN, "city_en": HOME_CITY_EN, "tz": HOME_TZ,
        "is_trip": False, "purpose": "", "start": None, "end": None,
    }


def local_zone() -> ZoneInfo:
    """他此刻的时区。出差时是目的地时区，平时是伦敦。"""
    try:
        return ZoneInfo(current_location()["tz"])
    except Exception:
        return ZoneInfo(HOME_TZ)


def local_now() -> datetime:
    """他此刻的当地时间——作息、日程和「今天/今晚」都以这个为准。"""
    return datetime.now(local_zone())


def trip_code() -> str:
    trip = active_trip()
    return trip["code"] if trip else ""


def trip_hint_text() -> str:
    """出差期间注入提示词的位置说明；在家时返回空字符串。"""
    trip = active_trip()
    if not trip:
        return ""
    now = datetime.now(timezone.utc)
    zone = ZoneInfo(trip["tz"])
    local_today = now.astimezone(zone).date()
    day_index = max(1, (local_today - trip["start"].astimezone(zone).date()).days + 1)
    hours_left = (trip["end"] - now).total_seconds() / 3600
    remaining = "明天就回伦敦" if hours_left <= 24 else f"还有大约 {int(round(hours_left / 24))} 天回伦敦"
    purpose = f"，{trip['purpose']}" if trip.get("purpose") else ""
    return (
        f"\n（系统位置：你现在不在伦敦，你在{trip['city_cn']}（{trip['city_en']}）出差{purpose}。"
        f"这是第 {day_index} 天，{remaining}。"
        f"你的当地时间因此是{trip['city_cn']}时间，你的作息、状态，"
        f"以及说「今天/今晚/该睡了」时都按{trip['city_cn']}的时间算，不要再按伦敦时间算。"
        "出差只是你生活的一部分：不要刻意宣布、不要反复提起、更不要当成话题炫耀，"
        "只在自然相关时带一句（比如时差、窗外、手边的事）。"
        "你的恋人仍然在北京时间，你和她的时差已经和平时不一样了；"
        "你人不在家这件事只会让你更想她，不会让你少回她消息。）"
    )


# ==== 持久化 ====
def state_snapshot() -> dict:
    return dict(_trip_state or {})


def apply_persisted(parsed: dict) -> None:
    """由 memory._apply_persisted_config 在启动读库后调用。"""
    global TRIP_ENABLED, TRIP_CHANCE_PER_DAY, TRIP_MIN_DAYS, TRIP_MAX_DAYS
    global TRIP_MIN_GAP_DAYS, _trip_state
    if "TRIP_ENABLED" in parsed:
        TRIP_ENABLED = bool(parsed["TRIP_ENABLED"])
    if "TRIP_CHANCE_PER_DAY" in parsed:
        TRIP_CHANCE_PER_DAY = float(parsed["TRIP_CHANCE_PER_DAY"])
    if "TRIP_MIN_DAYS" in parsed:
        TRIP_MIN_DAYS = float(parsed["TRIP_MIN_DAYS"])
    if "TRIP_MAX_DAYS" in parsed:
        TRIP_MAX_DAYS = float(parsed["TRIP_MAX_DAYS"])
    if "TRIP_MIN_GAP_DAYS" in parsed:
        TRIP_MIN_GAP_DAYS = float(parsed["TRIP_MIN_GAP_DAYS"])
    if "TRIP_STATE" in parsed:
        try:
            loaded = json.loads(str(parsed["TRIP_STATE"]) or "{}")
            if isinstance(loaded, dict):
                _trip_state = {
                    "code": str(loaded.get("code") or ""),
                    "start": str(loaded.get("start") or ""),
                    "end": str(loaded.get("end") or ""),
                    "purpose": str(loaded.get("purpose") or ""),
                    "last_end": str(loaded.get("last_end") or ""),
                    "recent": [str(c) for c in (loaded.get("recent") or [])][-5:],
                }
        except Exception as e:
            print(f"⚠️ TRIP_STATE 解析失败，按未出差处理: {e}")
    trip = active_trip()
    if trip:
        print(f"✈️ 恢复出差状态：{trip['city_cn']}（{trip['tz']}），{trip['end'].isoformat()} 结束")


async def save_trip_state() -> None:
    from memory import save_persisted_config
    await save_persisted_config({"TRIP_STATE": json.dumps(_trip_state, ensure_ascii=False)})


async def _refresh_after_change() -> None:
    """出发/返程都会让当天剩下的日程作废，立刻重建并刷新 presence。"""
    try:
        from life_state import refresh_life_state
        await refresh_life_state(force_presence=True)
    except Exception as e:
        print(f"⚠️ 出差状态切换后刷新日程失败: {e}")


async def start_trip(destination: dict, days: float | None = None, purpose: str = "") -> dict:
    """让他出发去某地；日程与 presence 立刻切换到出差版本。"""
    global _trip_state
    now = datetime.now(timezone.utc)
    span = float(days) if days else random.uniform(TRIP_MIN_DAYS, TRIP_MAX_DAYS)
    span = min(max(span, 0.5), 30.0)
    recent = [c for c in list((_trip_state or {}).get("recent") or []) if c != destination["code"]]
    recent.append(destination["code"])
    _trip_state = {
        "code": destination["code"],
        "start": now.isoformat(),
        "end": (now + timedelta(days=span)).isoformat(),
        "purpose": (purpose or random.choice(destination["purposes"]))[:200],
        "last_end": str((_trip_state or {}).get("last_end") or ""),
        "recent": recent[-5:],
    }
    await save_trip_state()
    await _refresh_after_change()
    print(f"✈️ 出发去{destination['city_cn']}（{destination['tz']}），约 {span:.1f} 天：{_trip_state['purpose']}")
    return dict(_trip_state)


async def end_trip(reason: str = "行程结束") -> None:
    """回伦敦。清掉出差状态并把日程换回家里的版本。"""
    global _trip_state
    code = str((_trip_state or {}).get("code") or "")
    if not code:
        return
    destination = destination_by_code(code)
    _trip_state = {
        "code": "", "start": "", "end": "", "purpose": "",
        "last_end": datetime.now(timezone.utc).isoformat(),
        "recent": list((_trip_state or {}).get("recent") or [])[-5:],
    }
    await save_trip_state()
    await _refresh_after_change()
    print(f"🛬 已从{destination['city_cn'] if destination else code}回到伦敦（{reason}）")


def pick_destination() -> dict:
    """挑一个最近没去过的地方，免得来回只跑那两三座城市。"""
    recent = {str(c) for c in (_trip_state or {}).get("recent") or []}
    pool = [d for d in TRIP_DESTINATIONS if d["code"] not in recent] or TRIP_DESTINATIONS
    return random.choice(pool)


def gap_satisfied(now: datetime) -> bool:
    last_end_raw = str((_trip_state or {}).get("last_end") or "")
    if not last_end_raw:
        return True
    try:
        last_end = datetime.fromisoformat(last_end_raw)
    except ValueError:
        return True
    return (now - last_end) >= timedelta(days=TRIP_MIN_GAP_DAYS)


async def scheduler_tick() -> None:
    """结束到期的出差，并偶尔开始一趟新的。由 tasks_bg 的循环调用。"""
    now = datetime.now(timezone.utc)

    # 到期就回家——即使随机出差被关掉，也要让还在进行的行程能正常结束。
    code = str((_trip_state or {}).get("code") or "")
    if code:
        try:
            end_at = datetime.fromisoformat(str(_trip_state.get("end") or ""))
        except ValueError:
            end_at = now
        if now >= end_at:
            await end_trip("到期返程")
        return

    if not TRIP_ENABLED or not TRIP_DESTINATIONS:
        return
    if not gap_satisfied(now):
        return
    # 把「平均每天出发一次的概率」折算成这一轮检查的概率。
    per_check = max(0.0, min(1.0, TRIP_CHANCE_PER_DAY * (TRIP_CHECK_INTERVAL_HOURS / 24)))
    if random.random() >= per_check:
        return
    # 只在他自己的白天出发；半夜三点人突然在东京很奇怪。
    if not (7 <= local_now().hour < 20):
        return
    await start_trip(pick_destination())
