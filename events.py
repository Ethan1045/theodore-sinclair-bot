"""Discord 事件处理器：on_ready、消息、反应与 presence 事件。"""
import asyncio
import base64
import random
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import discord

import config
import state
from client import discord_client, slash_tree
from ai_client import call_ai, api_runtime
from history import (
    get_history, history_key_for, trim_history, load_all_histories, _msg_to_plain_text,
    ensure_conversation_events_table, record_conversation_event,
)
from memory import (
    ensure_user_notes_table,
    ensure_users_table,
    ensure_daily_summaries_table,
    ensure_bot_config_table,
    ensure_reminders_table,
    load_persisted_config,
    extract_and_save_memory,
)


from history import ensure_conversation_history_table
from presence import (
    _TYPE_MAP,
    generate_presence,
    try_explicit_activity_sync,
    try_keyword_presence_update,
    get_guild_emoji_hint,
    get_guild_sticker_hint,
    get_london_weather,
)
from directives import parse_bot_directives
from reply import send_ai_reply, _keep_typing
from db import db_conn, init_db_pool
import tasks_bg
import trips
from followups import ensure_followup_table, capture_partner_message
from life_state import ensure_life_schedule_table, refresh_life_state
from polls import ensure_poll_followup_table
from relationship_emotion import is_wilted_rose_question
from relationship_state import (
    apply_relationship_boundary,
    ensure_relationship_emotion_table,
    relationship_prompt,
    update_relationship_from_reply,
)


async def _partner_owner_interaction_check(interaction: discord.Interaction) -> bool:
    """T.S. 的所有显式指令只允许恋人本人使用。"""
    if interaction.user.id == config.PARTNER_USER_ID:
        return True
    if interaction.type is discord.InteractionType.autocomplete:
        return False
    if not interaction.response.is_done():
        await interaction.response.send_message("这个 Bot 只对恋人开放。", ephemeral=True)
    return False


# CommandTree 会在 slash command、autocomplete 和右键菜单之前调用这个检查。
slash_tree.interaction_check = _partner_owner_interaction_check


def _is_partner_topic_channel(channel) -> bool:
    channel_id = getattr(channel, "id", None)
    return channel_id in (
        set(tasks_bg.THEODORE_PROACTIVE_TOPIC_CHANNEL_IDS)
        | set(tasks_bg.THEODORE_AUTO_POLL_CHANNEL_IDS)
    )


def _with_ephemeral(hist: list, target_msg: dict, ephemeral_text: str) -> list:
    """返回「仅用于本次请求」的历史副本：把一次性运行时提示拼到 target_msg 的
    文本部分上，但绝不修改存入历史的原始消息（历史只保留用户真正说的话）。
    target_msg 已被裁剪掉时原样返回。"""
    if not ephemeral_text:
        return hist
    out: list = []
    patched = False
    for m in hist:
        if m is target_msg and not patched:
            content = m.get("content")
            if isinstance(content, list):
                new_content = []
                injected = False
                for part in content:
                    if not injected and isinstance(part, dict) and part.get("type") == "text":
                        new_content.append({"type": "text", "text": part.get("text", "") + ephemeral_text})
                        injected = True
                    else:
                        new_content.append(part)
                if not injected:
                    new_content = [{"type": "text", "text": ephemeral_text}] + new_content
                out.append({"role": m.get("role", "user"), "content": new_content})
            else:
                out.append({"role": m.get("role", "user"), "content": str(content or "") + ephemeral_text})
            patched = True
        else:
            out.append(m)
    return out


def _media_description(message: discord.Message) -> str:
    """Return a compact textual description for media that may need vision."""
    parts: list[str] = []
    for attachment in getattr(message, "attachments", ()):
        parts.append(f"附件 {attachment.filename}")
    for sticker in getattr(message, "stickers", ()):
        fmt = getattr(getattr(sticker, "format", None), "name", "unknown")
        parts.append(f"贴纸「{sticker.name}」（{fmt}）")
    return "、".join(parts)


async def _resolve_referenced_message(message: discord.Message) -> discord.Message | None:
    reference = getattr(message, "reference", None)
    if not reference or not reference.message_id:
        return None
    resolved = getattr(reference, "resolved", None)
    if isinstance(resolved, discord.Message):
        return resolved
    try:
        channel = message.channel
        ref_channel_id = getattr(reference, "channel_id", None)
        if ref_channel_id and ref_channel_id != getattr(channel, "id", None):
            channel = await discord_client.fetch_channel(ref_channel_id)
        return await channel.fetch_message(reference.message_id)
    except Exception as exc:
        print(f"读取引用消息失败: {exc}")
        return None


async def _recent_pins(channel, *, limit: int = 5) -> list[discord.Message]:
    if not hasattr(channel, "pins"):
        return []
    try:
        return [item async for item in channel.pins(limit=limit)]
    except Exception as exc:
        print(f"⚠️ 读取频道标注消息失败: {exc}")
        return []


def _compact_old_media(hist: list[dict]) -> bool:
    """Do not resend old base64 images on every later conversation turn."""
    changed = False
    for entry in hist:
        content = entry.get("content")
        if not isinstance(content, list):
            continue
        if not any(isinstance(p, dict) and p.get("type") == "image_url" for p in content):
            continue
        text = _msg_to_plain_text(entry)
        entry["content"] = (text + " （[图片]）").strip()
        changed = True
    return changed


async def _vision_parts(messages: list[discord.Message]) -> list[dict]:
    """Build request-only high-detail vision parts for attachments and stickers."""
    parts: list[dict] = []
    seen: set[tuple[str, int]] = set()

    async def add_bytes(raw: bytes, mime_type: str, label: str) -> None:
        if len(parts) >= config.MAX_VISION_IMAGES:
            return
        if len(raw) > config.MAX_IMAGE_BYTES:
            print(f"⚠️ 跳过超大视觉输入 {label}：{len(raw)} 字节")
            return
        encoded = base64.b64encode(raw).decode("ascii")
        parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime_type};base64,{encoded}",
                "detail": "high",
            },
        })

    for source in messages:
        for attachment in getattr(source, "attachments", ()):
            key = ("attachment", attachment.id)
            if key in seen or len(parts) >= config.MAX_VISION_IMAGES:
                continue
            seen.add(key)
            content_type = (getattr(attachment, "content_type", None) or "").split(";", 1)[0].lower()
            ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else ""
            if content_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                if ext not in {"png", "jpg", "jpeg", "gif", "webp"}:
                    continue
                content_type = "image/jpeg" if ext in {"jpg", "jpeg"} else f"image/{ext}"
            if attachment.size and attachment.size > config.MAX_IMAGE_BYTES:
                print(f"⚠️ 跳过超大图片 {attachment.filename}：{attachment.size} 字节")
                continue
            try:
                await add_bytes(await attachment.read(), content_type, attachment.filename)
            except Exception as exc:
                print(f"⚠️ 读取图片失败 {attachment.filename}: {exc}")

        for sticker in getattr(source, "stickers", ()):
            key = ("sticker", sticker.id)
            if key in seen or len(parts) >= config.MAX_VISION_IMAGES:
                continue
            seen.add(key)
            fmt = getattr(getattr(sticker, "format", None), "name", "unknown")
            if fmt == "lottie":
                # Lottie is JSON animation data, not a vision-compatible image.
                print(f"ℹ️ Lottie 贴纸仅传递名称: {sticker.name}")
                continue
            mime_type = "image/gif" if fmt == "gif" else "image/png"
            try:
                await add_bytes(await sticker.read(), mime_type, f"贴纸 {sticker.name}")
            except Exception as exc:
                print(f"⚠️ 读取贴纸失败 {sticker.name}: {exc}")
    return parts


def _attach_request_media(hist: list, target_msg: dict, media_parts: list[dict]) -> list:
    """Attach media only to the outbound request, never to persistent history."""
    if not media_parts:
        return hist
    target_index = next((i for i, entry in enumerate(hist) if entry is target_msg), None)
    if target_index is None:
        # _with_ephemeral replaces the current message with a request-only copy.
        target_index = next(
            (i for i in range(len(hist) - 1, -1, -1) if hist[i].get("role") == "user"),
            None,
        )
    if target_index is None:
        return hist
    out: list = []
    for index, entry in enumerate(hist):
        if index == target_index:
            content = entry.get("content")
            if isinstance(content, list):
                request_content = list(content)
            else:
                request_content = [{"type": "text", "text": str(content or "")}]
            request_content.extend(media_parts)
            out.append({"role": entry.get("role", "user"), "content": request_content})
        else:
            out.append(entry)
    return out


@discord_client.event
async def on_ready():
    state._ensure_locks()
    print('=======================================')
    print(f'✅ {discord_client.user} is online.')
    print('=======================================')
    state.partner_last_seen_online = datetime.now(timezone.utc)
    await init_db_pool()
    for _loop, _name in (
        (tasks_bg.proactive_dm_partner,       "proactive_dm_partner"),
        (tasks_bg.anniversary_check,        "anniversary_check"),
        (tasks_bg.daily_summary_task,       "daily_summary_task"),
        (tasks_bg.cleanup_cooldowns,        "cleanup_cooldowns"),
        (tasks_bg.cleanup_idle_histories,   "cleanup_idle_histories"),
        (tasks_bg.daily_occasion_check,     "daily_occasion_check"),
        (tasks_bg.partner_proactive_topic_check,"partner_proactive_topic_check"),
        (tasks_bg.partner_auto_poll_check,      "partner_auto_poll_check"),
        (tasks_bg.daily_status_card,        "daily_status_card"),
        (tasks_bg.cleanup_stale_forum_posts,"cleanup_stale_forum_posts"),
        (tasks_bg.rotate_presence,          "rotate_presence"),
        (tasks_bg.check_reminders,          "check_reminders"),
        (tasks_bg.persist_histories_task,   "persist_histories_task"),
        (tasks_bg.random_forum_post,        "random_forum_post"),
        (tasks_bg.forum_interaction,        "forum_interaction"),
        (tasks_bg.commitment_followup_check,"commitment_followup_check"),
        (tasks_bg.poll_followup_check,      "poll_followup_check"),
        (tasks_bg.trip_scheduler_check,     "trip_scheduler_check"),
    ):
        try:
            tasks_bg._attach_loop_error_handler(_loop, _name)
        except Exception as _e:
            print(f"⚠️ 给 {_name} 装异常 handler 失败: {_e}")
    await ensure_user_notes_table()
    await ensure_users_table()
    await ensure_daily_summaries_table()
    await ensure_bot_config_table()
    api_config_ready = False
    commands_ready = False
    try:
        await api_runtime.initialize()
        api_config_ready = bool(api_runtime.database_url)
        print(f"✅ API 运行时配置已载入：{len(api_runtime.profiles())} 条")
    except Exception as exc:
        print(f"⚠️ API 配置载入失败（{type(exc).__name__}）；不会使用环境变量，请检查数据库并刷新 /api配置。")
    await ensure_reminders_table()
    await ensure_conversation_history_table()
    await ensure_conversation_events_table()
    await ensure_followup_table()
    await ensure_life_schedule_table()
    await ensure_poll_followup_table()
    await ensure_relationship_emotion_table()
    await load_persisted_config()
    if tasks_bg.PARTNER_PROACTIVE_TOPIC_ENABLED:
        await tasks_bg.ensure_partner_proactive_topic_plan(datetime.now(ZoneInfo("Asia/Shanghai")))
    if tasks_bg.PARTNER_AUTO_POLL_ENABLED:
        await tasks_bg.ensure_partner_auto_poll_plan(datetime.now(ZoneInfo("Asia/Shanghai")))
    await load_all_histories()
    try:
        tasks_bg.cleanup_stale_forum_posts.change_interval(hours=tasks_bg.CLEANUP_INTERVAL_HOURS)
    except Exception:
        pass
    # 这两个周期也是可配置的，落库的值要在启动时生效。
    try:
        import presence as presence_mod
        tasks_bg.rotate_presence.change_interval(minutes=presence_mod.ROTATE_MINUTES)
        tasks_bg.proactive_dm_partner.change_interval(hours=tasks_bg.PROACTIVE_DM_INTERVAL_HOURS)
    except Exception as e:
        print(f"⚠️ 调整状态栏/主动私信周期失败: {e}")
    try:
        synced = await slash_tree.sync()
        commands_ready = True
        print(f"✅ Slash commands 已同步: {len(synced)} 条")
    except Exception as e:
        print(f"⚠️ Slash commands 同步失败: {e}")
    if not tasks_bg.proactive_dm_partner.is_running():
        tasks_bg.proactive_dm_partner.start()
    if not tasks_bg.rotate_presence.is_running():
        tasks_bg.rotate_presence.start()
    if not tasks_bg.cleanup_cooldowns.is_running():
        tasks_bg.cleanup_cooldowns.start()
    if not tasks_bg.cleanup_idle_histories.is_running():
        tasks_bg.cleanup_idle_histories.start()
    if not tasks_bg.check_reminders.is_running():
        tasks_bg.check_reminders.start()
    if not tasks_bg.persist_histories_task.is_running():
        tasks_bg.persist_histories_task.start()
    if not tasks_bg.daily_occasion_check.is_running():
        tasks_bg.daily_occasion_check.start()
    try:
        slot, _ = await refresh_life_state(force_presence=True)
        if slot.get("availability") != "asleep" and state.sleep_pending_messages:
            tasks_bg.start_wakeup_catchup()
    except Exception as e:
        print(f"⚠️ 初始日程状态设置失败: {e}")
    if not tasks_bg.partner_proactive_topic_check.is_running():
        tasks_bg.partner_proactive_topic_check.start()
    if not tasks_bg.partner_auto_poll_check.is_running():
        tasks_bg.partner_auto_poll_check.start()
    if not tasks_bg.daily_status_card.is_running():
        tasks_bg.daily_status_card.start()
    if not tasks_bg.cleanup_stale_forum_posts.is_running():
        tasks_bg.cleanup_stale_forum_posts.start()
    if not tasks_bg.anniversary_check.is_running():
        tasks_bg.anniversary_check.start()
    if not tasks_bg.daily_summary_task.is_running():
        tasks_bg.daily_summary_task.start()
    if not tasks_bg.random_forum_post.is_running():
        tasks_bg.random_forum_post.start()
    if not tasks_bg.forum_interaction.is_running():
        tasks_bg.forum_interaction.start()
    if not tasks_bg.commitment_followup_check.is_running():
        tasks_bg.commitment_followup_check.start()
    if not tasks_bg.poll_followup_check.is_running():
        tasks_bg.poll_followup_check.start()
    if not tasks_bg.trip_scheduler_check.is_running():
        tasks_bg.trip_scheduler_check.start()
    if api_config_ready and commands_ready:
        print("DEPLOYMENT_READY service=theodore-sinclair-bot")


@discord_client.event
async def on_typing(channel, user, when):
    """Keep the merge window open while the same person is visibly still composing."""
    if (
        getattr(user, "bot", False)
        or getattr(user, "id", None) != config.PARTNER_USER_ID
    ):
        return
    key = (getattr(channel, "id", 0), getattr(user, "id", 0))
    state.user_typing_at[key] = asyncio.get_running_loop().time()


@discord_client.event
async def on_message(message):
    if message.author == discord_client.user:
        return
    is_pin_notice = message.type == discord.MessageType.pins_add
    is_dm = isinstance(message.channel, discord.DMChannel)
    # 公开模板保留原有社区互动：恋人之外的人仍可在服务器里 @ / 引用 T.S.，
    # 但身份、冷却、回应概率和私信白名单继续彼此隔离。
    if is_dm and not is_pin_notice and message.author.id not in config.DM_WHITELIST_IDS:
        return
    if getattr(message.channel, "id", None) in config.SILENT_CHANNEL_IDS:
        return

    user_input = message.content.replace(f'<@{discord_client.user.id}>', '').strip()
    if is_pin_notice:
        user_input = "（刚刚通过右键菜单标注了一条消息）"
    is_bot = message.author.bot and not is_pin_notice
    # 「标注消息」由 Bump 代为执行，但在 T.S. 的私人频道里它代表 恋人 的动作。
    is_partner = (message.author.id == config.PARTNER_USER_ID) or is_pin_notice
    is_partner_friend = (message.author.id in config.PARTNER_FRIEND_IDS)

    relationship_emotion = None
    if is_partner and not is_pin_notice:
        relationship_emotion = await apply_relationship_boundary(
            message.author.id, user_input
        )

    if is_partner and user_input and not is_pin_notice:
        state.spawn_bg(
            capture_partner_message(message, user_input),
            name=f"commitment-capture:{message.id}",
        )

    def is_guild_admin(msg: discord.Message) -> bool:
        if not msg.guild:
            return False
        perms = getattr(msg.author, "guild_permissions", None)
        if not perms:
            return False
        return bool(
            perms.administrator
            or perms.manage_guild
            or perms.manage_channels
            or perms.manage_threads
            or perms.manage_messages
        )

    is_mentioned = discord_client.user in message.mentions
    is_named = any(name in user_input.lower() for name in [
        "t.s.", "t.s", "theodore", "沈玘言", "玘言", "daddy",
        "爹", "爹地", "爹爹", "老公", "sinclair",
        "玘", "theo", "哥哥", "老公王",
    ])

    quoted_content = ""
    is_quoting_bot = False
    ref_msg = await _resolve_referenced_message(message)
    if is_pin_notice and ref_msg is None:
        pins = await _recent_pins(message.channel, limit=1)
        ref_msg = pins[0] if pins else None
    reference_messages: list[discord.Message] = []
    if ref_msg:
        reference_messages.append(ref_msg)
        quoted_author = ref_msg.author.display_name
        media_desc = _media_description(ref_msg)
        quoted_text = ref_msg.content or (f"（{media_desc}）" if media_desc else "（无文字内容）")
        action_name = "标注" if is_pin_notice else "引用"
        quoted_content = (
            f"\n\n（系统提示：对方刚刚{action_name}的消息所在频道ID为 {ref_msg.channel.id}，"
            f"该消息ID为 {ref_msg.id}。原作者：{quoted_author}，原内容：「{quoted_text}」。"
            + (f"媒体：{media_desc}。" if media_desc else "")
            + "如果指令要求删/置顶这条消息，请精准使用这两个ID。）"
        )
        if ref_msg.author.id == discord_client.user.id:
            is_quoting_bot = True

    if is_bot:
        if is_mentioned or is_quoting_bot:
            if random.random() > 0.15:
                return
        else:
            return

    if not is_partner and not is_mentioned:
        now_dt = datetime.now(timezone.utc)
        last = state.user_cooldowns.get(message.author.id)
        if last and (now_dt - last).total_seconds() < config.COOLDOWN_SECONDS:
            return
        state.user_cooldowns[message.author.id] = now_dt

    should_send_to_brain = False
    in_quiet_channel = (
        not is_dm
        and getattr(message.channel, "id", None) in config.QUIET_CHANNEL_IDS
    )
    quiet_mult = config.QUIET_CHANNEL_FACTOR if in_quiet_channel else 1.0
    # ==== 睡眠模式判定 ====
    is_sleeping, sleep_phase = config.is_sleep_time()
    if state.current_life_slot:
        is_sleeping = state.current_life_slot.get("availability") == "asleep"
        if is_sleeping:
            local_hour = config.get_london_hour()
            sleep_phase = "deep" if 1 <= local_hour < 4 else "light"
    sleep_mode = ""  # 传给 AI 的睡眠状态提示

    if is_dm:
        if is_partner:
            should_send_to_brain = True
        else:
            should_send_to_brain = random.random() < 0.40
    elif is_mentioned:
        if is_partner:
            should_send_to_brain = True
        elif is_partner_friend:
            should_send_to_brain = True
        else:
            should_send_to_brain = random.random() < 0.20
    elif is_quoting_bot:
        if is_partner:
            should_send_to_brain = True
        elif is_partner_friend:
            should_send_to_brain = True
        else:
            should_send_to_brain = random.random() < 0.15
    elif is_named:
        if is_partner:
            should_send_to_brain = random.random() < (1.0 if not in_quiet_channel else 0.5)
        elif is_partner_friend:
            should_send_to_brain = random.random() < (1.0 if not in_quiet_channel else 0.4)
        else:
            should_send_to_brain = random.random() < 0.10 * quiet_mult
    elif is_partner:
        if _is_partner_topic_channel(message.channel):
            should_send_to_brain = True
        elif message.channel and getattr(message.channel, "id", None) == config.PARTNER_HOME_CHANNEL_ID:
            should_send_to_brain = random.random() < 0.55
        else:
            should_send_to_brain = random.random() < 0.20 * quiet_mult
    elif is_partner_friend:
        should_send_to_brain = random.random() < 0.05 * quiet_mult
    elif is_bot:
        should_send_to_brain = False
    else:
        should_send_to_brain = False

    # 🥀 是接口限流的技术标记，不应让模型误判成关系暗示。只有本来就会
    # 回应的消息才本地直答，避免在普通公屏里无条件插话。
    if should_send_to_brain and is_wilted_rose_question(user_input):
        await message.reply("因为并发太高所以429了。", mention_author=False)
        return

    # 睡眠模式：降低回复概率，或改为挂表情
    if is_sleeping and should_send_to_brain:
        async def _track_sleep_pending(drowsy: bool):
            await tasks_bg.queue_sleep_pending({
                "channel_id": message.channel.id,
                "message_id": message.id,
                "user_id": message.author.id,
                "display_name": message.author.display_name,
                "text": user_input or "(无文字)",
                "is_dm": is_dm,
                "ts": datetime.now(timezone.utc).isoformat(),
                "drowsy": drowsy,
            })

        if sleep_phase == "deep":
            if is_partner and (is_mentioned or is_dm):
                # 深夜被 恋人 叫醒，小概率迷糊回复
                if random.random() < 0.25:
                    sleep_mode = "drowsy_deep"
                    await _track_sleep_pending(True)
                else:
                    # 挂个表情表示睡着了，睡醒后再自然补回。
                    try:
                        sleepy_emojis = ["😴", "💤", "🫠", "😶‍🌫️"]
                        await message.add_reaction(random.choice(sleepy_emojis))
                    except Exception:
                        pass
                    await _track_sleep_pending(False)
                    should_send_to_brain = False
            else:
                # 深夜其他人：直接不回
                should_send_to_brain = False
        elif sleep_phase == "light":
            if is_partner:
                # 浅睡被 恋人 叫：较高概率迷糊回
                if random.random() < 0.55:
                    sleep_mode = "drowsy_light"
                    await _track_sleep_pending(True)
                else:
                    try:
                        await message.add_reaction(random.choice(["💤", "🫠"]))
                    except Exception:
                        pass
                    await _track_sleep_pending(False)
                    should_send_to_brain = False
            elif is_mentioned:
                # 浅睡被 @ ：偶尔回
                if random.random() < 0.20:
                    sleep_mode = "drowsy_light"
                else:
                    should_send_to_brain = False
            else:
                should_send_to_brain = False

    if not should_send_to_brain:
        return

    merged_messages = [message]
    if (user_input or message.attachments or message.stickers) and not is_pin_notice:
        state._ensure_locks()
        merge_key = (getattr(message.channel, "id", 0), message.author.id)
        # Sending a message ends the previous typing burst. Only a new gateway
        # typing event after this point may extend this batch.
        state.user_typing_at.pop(merge_key, None)
        async with state._merge_lock:
            merge_state = state._merge_state.get(merge_key)
            if merge_state is None:
                merge_state = {"seq": 0, "messages": []}
                state._merge_state[merge_key] = merge_state
            merge_state["seq"] += 1
            merge_state["messages"].append(message)
            my_seq = merge_state["seq"]
            batch_size = len(merge_state["messages"])
        if batch_size < config.MERGE_MAX_BATCH:
            try:
                await asyncio.sleep(config.MERGE_WINDOW_SEC)
                loop = asyncio.get_running_loop()
                wait_started = loop.time() - config.MERGE_WINDOW_SEC
                while loop.time() - wait_started < config.TYPING_MERGE_MAX_SEC:
                    last_typing = state.user_typing_at.get(merge_key)
                    if last_typing is None or loop.time() - last_typing >= config.TYPING_GRACE_SEC:
                        break
                    await asyncio.sleep(min(0.5, config.TYPING_GRACE_SEC))
            except asyncio.CancelledError:
                raise
        async with state._merge_lock:
            cur_state = state._merge_state.get(merge_key)
            if cur_state is None or cur_state["seq"] != my_seq:
                return
            merged_messages = cur_state["messages"]
            state._merge_state.pop(merge_key, None)
        if len(merged_messages) > 1:
            parts: list[str] = []
            for m in merged_messages:
                t = m.content.replace(f'<@{discord_client.user.id}>', '').strip()
                if t:
                    parts.append(t)
                merged_ref = await _resolve_referenced_message(m)
                if merged_ref and all(merged_ref.id != old.id for old in reference_messages):
                    reference_messages.append(merged_ref)
                    desc = _media_description(merged_ref)
                    body = merged_ref.content or (f"（{desc}）" if desc else "（无文字内容）")
                    quoted_content += (
                        f"\n\n（系统提示：合并消息中还引用了 {merged_ref.author.display_name} "
                        f"的消息 ID={merged_ref.id}：「{body}」。）"
                    )
            if parts:
                user_input = " ｜ ".join(parts)
            print(f"📦 合并 {len(merged_messages)} 条消息 → 1 次 AI 调用")

    _stop_typing = asyncio.Event()
    asyncio.create_task(_keep_typing(message.channel, _stop_typing))

    current_media_desc = "、".join(filter(None, (_media_description(m) for m in merged_messages)))
    text_to_record = user_input if user_input else "（默默看着你，没说话）"
    if current_media_desc:
        text_to_record += f"\n（这条消息包含：{current_media_desc}）"
    text_to_record += quoted_content

    if is_bot:
        speaker_tag = f"[Bot · {message.author.display_name}，用户ID: {message.author.id}]"
    elif is_pin_notice:
        speaker_tag = "[恋人 通过右键菜单标注消息]"
    elif is_partner:
        speaker_tag = f"[恋人（用户ID: {config.PARTNER_USER_ID}）]"
    elif is_partner_friend:
        speaker_tag = (
            f"[{message.author.display_name} · 恋人的朋友，不是恋人本人，"
            f"用户ID: {message.author.id}]"
        )
    else:
        speaker_tag = f"[{message.author.display_name} · 陌生人，不是恋人也不是她的朋友，用户ID: {message.author.id}]"

    # 持久化进历史的，只有用户真正说的内容（speaker tag + 正文 + 引用）。
    message_with_name = f"{speaker_tag} 说：{text_to_record}"

    # 一次性运行时提示（系统隐秘提示/时间/emoji 清单/记忆/presence 等）只拼进
    # 「本次请求」，不写入历史，避免在后续每一轮里被反复携带、重复计费。
    ephemeral_parts: list[str] = []

    is_home_channel = (
        not is_dm
        and (
            getattr(message.channel, "id", None) == config.PARTNER_HOME_CHANNEL_ID
            or _is_partner_topic_channel(message.channel)
        )
    )

    if is_dm:
        ephemeral_parts.append("\n（系统隐秘提示：⚠️ 这是私聊频道！你语气可以更柔软，且绝对不能使用 [IGNORE] 潜水。）")
    elif is_mentioned:
        ephemeral_parts.append("\n（系统隐秘提示：对方在群里明确 @ 了你，你被强行唤醒，必须给予实质性回应，绝对不能使用 [IGNORE]。）")
    elif is_quoting_bot:
        if is_partner:
            ephemeral_parts.append("\n（系统隐秘提示：恋人引用了你说过的话，必须现身回应，绝对不能使用 [IGNORE]。）")
        elif is_partner_friend:
            ephemeral_parts.append("\n（系统隐秘提示：恋人的朋友引用了你说的话。既然代码决定让你现身了，请给个自然的回应，绝对不要使用 [IGNORE]。）")
        else:
            ephemeral_parts.append("\n（系统隐秘提示：有人引用了你的消息，请简短回应，尽量不要使用 [IGNORE]。）")
    elif is_named:
        if is_partner:
            ephemeral_parts.append("\n（系统隐秘提示：恋人提到了你或触发了专属称呼，你必须现身回应她，绝对不能使用 [IGNORE]。）")
        elif is_partner_friend:
            ephemeral_parts.append("\n（系统隐秘提示：恋人的朋友提到了你。请给个高冷但礼貌的回应，绝对不要使用 [IGNORE]。）")
        else:
            ephemeral_parts.append("\n（系统隐秘提示：路人提到了你，随便回一句，不要使用 [IGNORE]。）")
    elif is_bot:
        ephemeral_parts.append("\n（系统隐秘提示：这是一个Bot。你可以简短冷淡地回一句，或者直接 [IGNORE]。）")
    elif is_partner:
        ephemeral_parts.append("\n（系统隐秘提示：恋人在公共频道普通聊天，而你此刻正好想顺势插进话题里。请自然地参与对话，展现你的偏爱，绝对不能使用 [IGNORE]。）")
    elif is_partner_friend:
        ephemeral_parts.append("\n（系统隐秘提示：恋人的朋友在闲聊，你偶然决定接一句话。请保持你的风格简短回应，绝对不能使用 [IGNORE]。）")
    else:
        ephemeral_parts.append("\n（系统隐秘提示：你偶然决定回应这句话。没兴趣的话也可以 [IGNORE]。）")

    if is_home_channel:
        ephemeral_parts.append(
            "\n（系统隐秘提示：⭐ 当前频道是你和恋人日常驻扎的「主场」频道，"
            "气氛接近两个人的「客厅」，比一般公屏更松弛、更像私聊。"
            "你可以更自然地接她的话、更主动开口，但格式仍然遵守双语 + [SPLIT]。"
            "如果说话的人是恋人，绝对不要 [IGNORE]。）"
        )

    if state.mandatory_instruction:
        ephemeral_parts.append(f"\n（⚠️ 提醒：当前有恋人下达的强制指令仍然有效：{state.mandatory_instruction}）")

    if is_partner and relationship_emotion is not None:
        ephemeral_parts.append(relationship_prompt(relationship_emotion))

    if (not is_dm) and message.guild and tasks_bg.should_opportunistic_post(user_input):
        ephemeral_parts.append(
            "\n（系统隐秘提示：如果你认为有必要，可以把刚才的话题整理成一条论坛帖子；"
            "但这应当非常少见。若你决定发帖，请在回复末尾追加："
            f" [ACTION]{{\"type\":\"CREATE_FORUM_POST\",\"channel_id\":{config.PROACTIVE_CHANNEL_ID},\"title\":\"标题\",\"content\":\"正文\"}}[/ACTION] 。）"
        )

    def _care_cooled(key: str) -> bool:
        last = state.care_reminder_last.get(key)
        if last is None:
            return True
        return (datetime.now(timezone.utc) - last).total_seconds() > config.CARE_REMINDER_COOLDOWN_HOURS * 3600

    _care_suppress = []
    if not _care_cooled("eat"):
        _care_suppress.append("吃饭/吃了没")
    if not _care_cooled("sleep"):
        _care_suppress.append("睡觉/睡了没")
    if _care_suppress:
        ephemeral_parts.append(f"\n（系统强制限制：本轮回复绝对不允许提及或暗示「{'、'.join(_care_suppress)}」相关的提醒。）")

    _coins_guild_id = str(message.guild.id) if message.guild else "dm"
    if config.DATABASE_URL and _coins_guild_id:
        try:
            async with db_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT level, balance FROM users WHERE guild_id = %s AND user_id = %s",
                        (_coins_guild_id, str(message.author.id))
                    )
                    row = await cur.fetchone()
            if row:
                p_level, p_balance = row[0], row[1]
                ephemeral_parts.append(
                    f"\n（系统隐秘提示：当前和你聊天的玩家，在游戏系统中的等级是 Lv.{p_level}，"
                    f"拥有 {p_balance} 枚金币。你可以作为聊天背景自然提及，或看心情用 ADD_COINS 给她发零花钱。）"
                )
        except Exception as e:
            print(f"读取游戏数据失败: {e}")

    if message.guild:
        action_target = ref_msg if is_pin_notice and ref_msg else message
        ephemeral_parts.append(
            f"\n（系统提示：当前可操作消息ID={action_target.id}，所在频道ID={getattr(action_target.channel, 'id', '')}。"
            "如果你决定置顶/删除当前这条消息，直接复用这两个ID。）"
        )

    pinned_messages: list[discord.Message] = []
    if not is_pin_notice and any(word in user_input.lower() for word in ("标注", "置顶", "pinned", "pin")):
        pinned_messages = await _recent_pins(message.channel, limit=5)
        if pinned_messages:
            lines = []
            for pinned in pinned_messages:
                desc = _media_description(pinned)
                body = (pinned.content or (f"[{desc}]" if desc else "[无文字]")).replace("\n", " ")[:500]
                lines.append(f"- ID={pinned.id}，{pinned.author.display_name}：{body}")
            ephemeral_parts.append(
                "\n（系统提示：当前频道最近的标注/置顶消息如下：\n"
                + "\n".join(lines)
                + "\n用户说「标注的东西」时，应根据这些真实内容回应，不要猜。）"
            )

    ephemeral_parts.append("\n" + config.get_beijing_time_note())
    emoji_ctx = get_guild_emoji_hint(message.guild)
    if emoji_ctx:
        ephemeral_parts.append(emoji_ctx)
    sticker_ctx = get_guild_sticker_hint(message.guild)
    if sticker_ctx:
        ephemeral_parts.append(sticker_ctx)

    # 把她这句话传进去：聊到出差/城市/时差时才展开完整行程，平时只带最近一趟。
    life_ctx = state.life_hint_text(user_input)
    if life_ctx:
        ephemeral_parts.append(life_ctx)

    if is_partner and config.DATABASE_URL:
        from memory import fetch_memory_context
        mem_ctx = await fetch_memory_context(str(message.author.id), topic_hint=user_input)
        if mem_ctx:
            ephemeral_parts.append(mem_ctx)

    if is_partner:
        ph = state.presence_hint_text()
        if ph:
            ephemeral_parts.append(ph)

    # 睡眠模式：注入迷糊回复提示（深夜按他此刻所在地算，出差时是目的地的深夜）
    where_now = trips.current_location()["city_cn"]
    if sleep_mode == "drowsy_deep":
        ephemeral_parts.append(
            f"\n（系统隐秘提示：你现在在{where_now}，已经是深夜了，你本来已经睡着了但被消息吵醒。"
            "你迷迷糊糊的，回复极其简短——一两句话，可能有错字或断句不完整，像是半梦半醒摸到手机敲的。"
            "不要提你「被设定为睡觉」或任何系统机制，就自然地表现出困意。"
            "可以用省略号、断句、少量错字来体现。不需要双语，随便用一种语言就行。）"
        )
    elif sleep_mode == "drowsy_light":
        ephemeral_parts.append(
            f"\n（系统隐秘提示：你现在在{where_now}，很晚了快要睡着、或者刚刚醒来还没完全清醒。"
            "回复简短，带一点困意，像真人深夜/清晨发消息那样——句子可以短一些，语气比平时更软更随意。"
            "仍然保持双语格式，但可以更简短。）"
        )

    ephemeral_text = "".join(ephemeral_parts)

    hist_key = history_key_for(message=message)
    bucket_lock = state.get_bucket_lock(hist_key)
    stored_user_msg = {"role": "user", "content": message_with_name}
    async with bucket_lock:
        hist = get_history(hist_key)
        if _compact_old_media(hist):
            state.mark_history_dirty(hist_key)
        hist.append(stored_user_msg)
    state.spawn_bg(
        record_conversation_event(hist_key, "user", _msg_to_plain_text(stored_user_msg)),
        name=f"daily-event:user:{message.id}",
    )
    await trim_history(hist_key)

    if is_partner:
        state.last_partner_activity_at = datetime.now(timezone.utc)

    # 仅本次请求使用：把一次性提示拼到刚存的这条消息上，不写回历史。
    hist_for_ai = _with_ephemeral(hist, stored_user_msg, ephemeral_text)
    media_sources = merged_messages + reference_messages + pinned_messages
    media_parts = await _vision_parts(media_sources)
    hist_for_ai = _attach_request_media(hist_for_ai, stored_user_msg, media_parts)

    try:
        raw_bot_reply = await call_ai(hist_for_ai)
        if (is_mentioned or is_pin_notice) and "[IGNORE]" in raw_bot_reply.upper():
            import copy
            retry_prompt = copy.deepcopy(hist_for_ai)
            last_msg = retry_prompt[-1]["content"]
            reason = "明确@了你" if is_mentioned else "刚刚标注了一条消息"
            append_text = (
                f"\n（⚠️ 系统强制：你刚才输出了[IGNORE]，但对方{reason}，这不被允许。"
                "你现在必须根据那条消息给出回应，哪怕只一句话。）"
            )
            if isinstance(last_msg, list):
                for item in last_msg:
                    if item.get("type") == "text":
                        item["text"] += append_text
                        break
            else:
                retry_prompt[-1]["content"] += append_text
            raw_bot_reply = await call_ai(retry_prompt)
        if is_partner:
            raw_bot_reply, relationship_emotion = await update_relationship_from_reply(
                message.author.id, raw_bot_reply
            )
        _stop_typing.set()
        # Pin system notices cannot reliably be replied to. Anchor the response to
        # the actual pinned message so Discord renders a useful reference.
        reply_target = ref_msg if is_pin_notice and ref_msg else message
        await send_ai_reply(raw_bot_reply, reply_target, message.channel)
        if is_partner and user_input and not is_pin_notice:
            await try_explicit_activity_sync(user_input)
            await try_keyword_presence_update(user_input)
            state.spawn_bg(
                extract_and_save_memory(str(message.author.id), user_input),
                name=f"memory:{message.author.id}",
            )
    except Exception as e:
        _stop_typing.set()
        err_msg = f"❌ AI 调用最终失败: {type(e).__name__}: {e}"
        print(err_msg)
        async with bucket_lock:
            if hist and hist[-1].get("role") == "user":
                hist.pop()
        state.mark_history_dirty(hist_key)
        if is_partner or is_mentioned:
            try:
                await message.add_reaction("🥀")
            except Exception:
                pass
    finally:
        _stop_typing.set()


@discord_client.event
async def on_message_edit(before, after):
    if after.author == discord_client.user or after.author.bot:
        return
    if before.content == after.content:
        return
    if after.author.id != config.PARTNER_USER_ID:
        return
    if not (isinstance(after.channel, discord.DMChannel) or _is_partner_topic_channel(after.channel)):
        return
    if random.random() > 0.25:
        return

    edit_note = (
        f"[恋人（用户ID: {config.PARTNER_USER_ID}）] 刚刚修改了一条消息。"
        f"修改前：「{before.content}」，修改后：「{after.content}」。"
        "你注意到了这个变化，可以用你的风格评论一句或假装没看见（[IGNORE]）。"
    )
    hist_key = history_key_for(message=after)
    bucket_lock = state.get_bucket_lock(hist_key)
    async with bucket_lock:
        hist = get_history(hist_key)
        hist.append({"role": "user", "content": edit_note})
    await trim_history(hist_key)

    async with after.channel.typing():
        try:
            raw_reply = await call_ai(hist)
            await send_ai_reply(raw_reply, after, after.channel)
        except Exception as e:
            print(f"编辑监听报错: {e}")


async def _handle_trash_reaction(payload) -> bool:
    """🗑️ 反应：从对话历史中移除对应消息，同步数据库。DM和频道均可用。"""
    if str(payload.emoji) != "🗑️":
        return False
    if payload.user_id != config.PARTNER_USER_ID:
        return False
    try:
        channel = await discord_client.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
    except Exception as e:
        print(f"⚠️ 🗑️ 获取消息失败: {e}")
        return True
    search_content = message.content.replace(f'<@{discord_client.user.id}>', '').strip()
    if not search_content or len(search_content) < 2:
        try:
            await message.add_reaction("❌")
        except Exception:
            pass
        return True
    is_bot_msg = message.author.id == discord_client.user.id
    target_role = "assistant" if is_bot_msg else "user"
    hist_key = history_key_for(channel=channel)
    bucket_lock = state.get_bucket_lock(hist_key)
    removed = False
    async with bucket_lock:
        hist = state._histories.get(hist_key)
        if hist:
            for i in range(len(hist) - 1, 0, -1):
                entry = hist[i]
                if entry.get("role") != target_role:
                    continue
                entry_text = _msg_to_plain_text(entry)
                if search_content in entry_text:
                    hist.pop(i)
                    removed = True
                    break
    if removed:
        state.mark_history_dirty(hist_key)
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
        if is_bot_msg:
            try:
                await message.delete()
            except Exception:
                pass
        print(f"🗑️ 已从历史删除 (bucket={hist_key}, bot={is_bot_msg})")
    else:
        try:
            await message.add_reaction("❓")
        except Exception:
            pass
    return True


@discord_client.event
async def on_raw_reaction_add(payload):
    if payload.user_id == discord_client.user.id:
        return
    if await _handle_trash_reaction(payload):
        return
    if payload.guild_id is None:
        return
    if payload.channel_id in config.SILENT_CHANNEL_IDS:
        return

    channel = await discord_client.fetch_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)

    if message.author.id != discord_client.user.id:
        return

    reactor_is_partner = (payload.user_id == config.PARTNER_USER_ID)
    reactor_is_friend = (payload.user_id in config.PARTNER_FRIEND_IDS)
    REACTION_REPLY_PROBABILITY = 0.70 if reactor_is_partner else (0.10 if reactor_is_friend else 0.35)

    if payload.user_id == config.PARTNER_USER_ID:
        reactor_name = f"恋人（用户ID: {config.PARTNER_USER_ID}）"
    elif reactor_is_friend:
        if payload.member:
            display = payload.member.display_name
        else:
            try:
                user = await discord_client.fetch_user(payload.user_id)
                display = user.display_name
            except Exception:
                display = "朋友"
        reactor_name = f"{display} · 恋人的朋友，不是恋人本人，用户ID: {payload.user_id}"
    else:
        if payload.member:
            display = payload.member.display_name
        else:
            try:
                user = await discord_client.fetch_user(payload.user_id)
                display = user.display_name
            except Exception:
                display = "某人"
        reactor_name = f"{display} · 陌生人，不是恋人也不是她的朋友，用户ID: {payload.user_id}"

    reaction_emoji = str(payload.emoji)

    if random.random() > REACTION_REPLY_PROBABILITY:
        return

    original_message_text = message.content.strip()
    fake_user_input = (
        f"（动作提示：{reactor_name} 刚刚对你发出的这条消息：[{original_message_text}] "
        f"偷偷点了一个 {reaction_emoji} 的表情反应，并没有说话。）"
    )
    message_with_name = f"[{reactor_name}] 动作：{fake_user_input}"
    message_content = [{"type": "text", "text": message_with_name}]

    hist_key = history_key_for(channel=channel)
    bucket_lock = state.get_bucket_lock(hist_key)
    async with bucket_lock:
        hist = get_history(hist_key)
        hist.append({"role": "user", "content": message_content})
    await trim_history(hist_key)

    async with channel.typing():
        try:
            raw_bot_reply = await call_ai(hist)
            clean_reply, messages_to_send, reaction_target, emojis_to_react, _action_matches = parse_bot_directives(raw_bot_reply)
            async with bucket_lock:
                if clean_reply:
                    hist.append({"role": "assistant", "content": clean_reply.replace('[SPLIT]', '\n')})
                else:
                    hist.append({"role": "assistant", "content": "（针对刚才的表情反应，只回挂了表情）"})
            await trim_history(hist_key)

            sent_message = None
            if messages_to_send:
                for i, msg_text in enumerate(messages_to_send):
                    if i > 0:
                        async with channel.typing():
                            await asyncio.sleep(min(1.0 + len(msg_text) * 0.02, 3.0))
                        sent_message = await channel.send(msg_text)
                    else:
                        sent_message = await message.reply(msg_text)

            if emojis_to_react:
                target_msg = message if reaction_target == "USER" or not sent_message else sent_message
                for emoji in emojis_to_react:
                    try:
                        await target_msg.add_reaction(emoji)
                    except Exception as e:
                        print(f"挂载表情失败：{e}")
        except Exception as e:
            print(f"监听系统报错：{e}")


@discord_client.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    if after.id != config.PARTNER_USER_ID:
        return
    was_offline = before.status == discord.Status.offline
    is_now_online = after.status not in (discord.Status.offline, discord.Status.invisible)
    if not (was_offline and is_now_online):
        return

    now = datetime.now(timezone.utc)
    if state.partner_last_seen_online and (now - state.partner_last_seen_online).total_seconds() < 3 * 3600:
        return
    state.partner_last_seen_online = now

    if random.random() > 0.10:
        return

    try:
        time_ctx = config.get_beijing_time_note()
        prompt = (
            f"（系统提示：{time_ctx} 恋人刚刚从离线状态上线了。你注意到了。"
            "你可以选择：① 给她发一条极短的私信；"
            "② 或者什么都不做 [IGNORE]。）"
        )
        temp_history = get_history(f"dm:{config.PARTNER_USER_ID}").copy()
        temp_history.append({"role": "user", "content": prompt})
        raw = await call_ai(temp_history)
        if "[IGNORE]" in raw.upper():
            return
        clean = re.sub(r'\[REACTION:.*?\]\n?', '', raw, flags=re.DOTALL)
        clean = re.sub(r'\[ACTION\].*?\[/ACTION\]\n?', '', clean, flags=re.DOTALL).strip()
        msgs = [m.strip() for m in clean.split('[SPLIT]') if m.strip()]
        partner_user = await discord_client.fetch_user(config.PARTNER_USER_ID)
        for msg_text in msgs:
            await partner_user.send(msg_text)
        print(f"✅ 感知到恋人上线，发送了私信")
    except Exception as e:
        print(f"⚠️ 恋人上线感知报错: {e}")
