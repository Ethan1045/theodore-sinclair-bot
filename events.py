"""Discord 事件处理器：on_ready, on_message, on_message_edit, on_member_join,
on_raw_reaction_add, on_presence_update。"""
import asyncio
import base64
import random
import re
from datetime import datetime, timezone

import discord

import config
import state
from client import discord_client, slash_tree
from ai_client import call_ai
from history import get_history, history_key_for, trim_history, load_all_histories
from memory import (
    ensure_user_notes_table,
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
)
from reply import send_ai_reply, _keep_typing
from db import db_conn, init_db_pool
import tasks_bg


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


@discord_client.event
async def on_ready():
    state._ensure_locks()
    print('=======================================')
    print(f'✅ {discord_client.user} is online.')
    print('=======================================')
    state.partner_last_seen_online = datetime.now(timezone.utc)
    await init_db_pool()
    for _loop, _name in (
        (tasks_bg.proactive_dm_partner,     "proactive_dm_partner"),
        (tasks_bg.anniversary_check,        "anniversary_check"),
        (tasks_bg.daily_summary_task,       "daily_summary_task"),
        (tasks_bg.cleanup_cooldowns,        "cleanup_cooldowns"),
        (tasks_bg.cleanup_idle_histories,   "cleanup_idle_histories"),
        (tasks_bg.daily_occasion_check,     "daily_occasion_check"),
        (tasks_bg.daily_status_card,        "daily_status_card"),
        (tasks_bg.cleanup_stale_forum_posts,"cleanup_stale_forum_posts"),
        (tasks_bg.rotate_presence,          "rotate_presence"),
        (tasks_bg.check_reminders,          "check_reminders"),
        (tasks_bg.persist_histories_task,   "persist_histories_task"),
    ):
        try:
            tasks_bg._attach_loop_error_handler(_loop, _name)
        except Exception as _e:
            print(f"⚠️ 给 {_name} 装异常 handler 失败: {_e}")
    await ensure_user_notes_table()
    await ensure_daily_summaries_table()
    await ensure_bot_config_table()
    await ensure_reminders_table()
    await ensure_conversation_history_table()
    await load_persisted_config()
    await load_all_histories()
    try:
        tasks_bg.cleanup_stale_forum_posts.change_interval(hours=tasks_bg.CLEANUP_INTERVAL_HOURS)
    except Exception:
        pass
    try:
        synced = await slash_tree.sync()
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
        text, activity_type = await generate_presence()
        await discord_client.change_presence(
            status=discord.Status.idle,
            activity=discord.Activity(type=activity_type, name=text)
        )
        kind = next((k for k, v in _TYPE_MAP.items() if v == activity_type), "playing")
        state.set_current_presence(kind, text, source="boot")
    except Exception as e:
        print(f"⚠️ 初始状态设置失败: {e}")
    if not tasks_bg.daily_status_card.is_running():
        tasks_bg.daily_status_card.start()
    if not tasks_bg.cleanup_stale_forum_posts.is_running():
        tasks_bg.cleanup_stale_forum_posts.start()
    if not tasks_bg.daily_summary_task.is_running():
        tasks_bg.daily_summary_task.start()
    if not tasks_bg.anniversary_check.is_running():
        tasks_bg.anniversary_check.start()


@discord_client.event
async def on_message(message):
    if message.author == discord_client.user:
        return
    if getattr(message.channel, "id", None) in config.SILENT_CHANNEL_IDS:
        return

    user_input = message.content.replace(f'<@{discord_client.user.id}>', '').strip()
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_bot = message.author.bot
    is_partner = bool(config.PARTNER_USER_ID) and (message.author.id == config.PARTNER_USER_ID)

    is_mentioned = discord_client.user in message.mentions
    is_named = any(name in user_input.lower() for name in [
        "t.s.", "t.s", "theodore", "沈玘言", "玘言", "daddy",
        "爹", "爹地", "爹爹", "老公", "sinclair",
        "玘", "theo", "哥哥",
    ])

    quoted_content = ""
    is_quoting_bot = False
    if message.reference and message.reference.message_id:
        try:
            ref_msg = await message.channel.fetch_message(message.reference.message_id)
            if ref_msg:
                quoted_author = ref_msg.author.display_name
                quoted_text = ref_msg.content or "（无文字内容）"
                quoted_content = (
                    f"\n\n（系统提示：对方引用的消息所在频道ID为 {ref_msg.channel.id}，"
                    f"该消息ID为 {ref_msg.id}。原作者：{quoted_author}，原内容：「{quoted_text}」。"
                    "如果指令要求删/置顶这条消息，请精准使用这两个ID。）"
                )
                if ref_msg.author.id == discord_client.user.id:
                    is_quoting_bot = True
        except Exception as e:
            print(f"读取引用消息失败: {e}")

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
    if is_dm:
        if is_partner:
            should_send_to_brain = True
        else:
            should_send_to_brain = random.random() < 0.40
    elif is_mentioned:
        if is_partner:
            should_send_to_brain = True
        else:
            should_send_to_brain = random.random() < 0.20
    elif is_quoting_bot:
        if is_partner:
            should_send_to_brain = True
        else:
            should_send_to_brain = random.random() < 0.15
    elif is_named:
        if is_partner:
            should_send_to_brain = random.random() < (1.0 if not in_quiet_channel else 0.5)
        else:
            should_send_to_brain = random.random() < 0.10 * quiet_mult
    elif is_partner:
        if message.channel and getattr(message.channel, "id", None) == config.PARTNER_HOME_CHANNEL_ID:
            should_send_to_brain = random.random() < 0.55
        else:
            should_send_to_brain = random.random() < 0.20 * quiet_mult
    elif is_bot:
        should_send_to_brain = False
    else:
        should_send_to_brain = False
    if not should_send_to_brain:
        return

    merged_attachments = list(message.attachments)
    if user_input or message.attachments:
        state._ensure_locks()
        merge_key = (getattr(message.channel, "id", 0), message.author.id)
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
            merged_attachments = []
            for m in merged_messages:
                t = m.content.replace(f'<@{discord_client.user.id}>', '').strip()
                if t:
                    parts.append(t)
                merged_attachments.extend(m.attachments)
            if parts:
                user_input = " ｜ ".join(parts)
            print(f"📦 合并 {len(merged_messages)} 条消息 → 1 次 AI 调用")

    _stop_typing = asyncio.Event()
    asyncio.create_task(_keep_typing(message.channel, _stop_typing))

    text_to_record = user_input if user_input else "（默默看着你，没说话）"
    text_to_record += quoted_content

    if is_bot:
        speaker_tag = f"[Bot · {message.author.display_name}，用户ID: {message.author.id}]"
    elif is_partner:
        speaker_tag = f"[她 · 你的恋人，用户ID: {config.PARTNER_USER_ID}]"
    else:
        speaker_tag = f"[{message.author.display_name} · 陌生人，不是你的恋人，用户ID: {message.author.id}]"

    # 持久化进历史的，只有用户真正说的内容（speaker tag + 正文 + 引用）。
    message_with_name = f"{speaker_tag} 说：{text_to_record}"

    # 一次性运行时提示只拼进「本次请求」，不写入历史。
    ephemeral_parts: list[str] = []

    is_home_channel = (
        not is_dm
        and config.PARTNER_HOME_CHANNEL_ID
        and getattr(message.channel, "id", None) == config.PARTNER_HOME_CHANNEL_ID
    )

    if is_dm:
        ephemeral_parts.append("\n（系统隐秘提示：⚠️ 这是私聊频道！你语气可以更柔软，且绝对不能使用 [IGNORE] 潜水。）")
    elif is_mentioned:
        ephemeral_parts.append("\n（系统隐秘提示：对方在群里明确 @ 了你，你被强行唤醒，必须给予实质性回应，绝对不能使用 [IGNORE]。）")
    elif is_quoting_bot:
        if is_partner:
            ephemeral_parts.append("\n（系统隐秘提示：她引用了你说过的话，必须现身回应，绝对不能使用 [IGNORE]。）")
        else:
            ephemeral_parts.append("\n（系统隐秘提示：有人引用了你的消息，请简短回应，尽量不要使用 [IGNORE]。）")
    elif is_named:
        if is_partner:
            ephemeral_parts.append("\n（系统隐秘提示：她提到了你或触发了专属称呼，你必须现身回应她，绝对不能使用 [IGNORE]。）")
        else:
            ephemeral_parts.append("\n（系统隐秘提示：路人提到了你，随便回一句，不要使用 [IGNORE]。）")
    elif is_bot:
        ephemeral_parts.append("\n（系统隐秘提示：这是一个 Bot。你可以简短冷淡地回一句，或者直接 [IGNORE]。）")
    elif is_partner:
        ephemeral_parts.append("\n（系统隐秘提示：她在公共频道普通聊天，而你此刻正好想顺势插进话题里。请自然地参与对话，展现你的偏爱，绝对不能使用 [IGNORE]。）")
    else:
        ephemeral_parts.append("\n（系统隐秘提示：你偶然决定回应这句话。没兴趣的话也可以 [IGNORE]。）")

    if is_home_channel:
        ephemeral_parts.append(
            "\n（系统隐秘提示：⭐ 当前频道是你和她日常驻扎的「主场」频道，"
            "气氛接近两个人的「客厅」，比一般公屏更松弛、更像私聊。"
            "你可以更自然地接她的话、更主动开口，但格式仍然遵守双语 + [SPLIT]。"
            "如果说话的人是她，绝对不要 [IGNORE]。）"
        )

    if state.mandatory_instruction:
        ephemeral_parts.append(f"\n（⚠️ 提醒：当前有她下达的强制指令仍然有效：{state.mandatory_instruction}）")

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

    if config.DATABASE_URL and message.guild:
        try:
            async with db_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT level, balance FROM users WHERE guild_id = %s AND user_id = %s",
                        (str(message.guild.id), str(message.author.id))
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
        ephemeral_parts.append(
            f"\n（系统提示：当前消息ID={message.id}，所在频道ID={getattr(message.channel, 'id', '')}。"
            "如果你决定置顶/删除当前这条消息，直接复用这两个ID。）"
        )

    ephemeral_parts.append("\n" + config.get_beijing_time_note())
    emoji_ctx = get_guild_emoji_hint(message.guild)
    if emoji_ctx:
        ephemeral_parts.append(emoji_ctx)

    if is_partner and config.DATABASE_URL:
        from memory import fetch_memory_context
        mem_ctx = await fetch_memory_context(str(message.author.id), topic_hint=user_input)
        if mem_ctx:
            ephemeral_parts.append(mem_ctx)

    if is_partner:
        ph = state.presence_hint_text()
        if ph:
            ephemeral_parts.append(ph)

    ephemeral_text = "".join(ephemeral_parts)

    message_content = [{"type": "text", "text": message_with_name}]

    if merged_attachments:
        for attachment in merged_attachments:
            if attachment.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                if attachment.size and attachment.size > config.MAX_IMAGE_BYTES:
                    print(f"⚠️ 跳过超大图片 {attachment.filename}：{attachment.size} 字节 > {config.MAX_IMAGE_BYTES}")
                    continue
                image_bytes = await attachment.read()
                if len(image_bytes) > config.MAX_IMAGE_BYTES:
                    print(f"⚠️ 跳过超大图片 {attachment.filename}：实际 {len(image_bytes)} 字节")
                    continue
                base64_encoded = base64.b64encode(image_bytes).decode('utf-8')
                ext = attachment.filename.lower().split('.')[-1]
                if ext == 'jpg':
                    ext = 'jpeg'
                mime_type = f"image/{ext}"
                message_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{base64_encoded}"}
                })

    hist_key = history_key_for(message=message)
    bucket_lock = state.get_bucket_lock(hist_key)
    stored_user_msg = {"role": "user", "content": message_content}
    async with bucket_lock:
        hist = get_history(hist_key)
        hist.append(stored_user_msg)
    await trim_history(hist_key)

    if is_partner:
        state.last_partner_activity_at = datetime.now(timezone.utc)

    hist_for_ai = _with_ephemeral(hist, stored_user_msg, ephemeral_text)

    try:
        raw_bot_reply = await call_ai(hist_for_ai)
        if is_mentioned and "[IGNORE]" in raw_bot_reply.upper():
            import copy
            retry_prompt = copy.deepcopy(hist_for_ai)
            last_msg = retry_prompt[-1]["content"]
            append_text = "\n（⚠️ 系统强制：你刚才输出了[IGNORE]，但对方明确@了你，这不被允许。你现在必须回应，哪怕只一句话。）"
            if isinstance(last_msg, list):
                for item in last_msg:
                    if item.get("type") == "text":
                        item["text"] += append_text
                        break
            else:
                retry_prompt[-1]["content"] += append_text
            raw_bot_reply = await call_ai(retry_prompt)
        _stop_typing.set()
        await send_ai_reply(raw_bot_reply, message, message.channel)
        if is_partner and user_input:
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
    if not config.PARTNER_USER_ID or after.author.id != config.PARTNER_USER_ID:
        return
    if random.random() > 0.25:
        return

    edit_note = (
        f"[她 · 你的恋人，用户ID: {config.PARTNER_USER_ID}] 刚刚修改了一条消息。"
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


@discord_client.event
async def on_member_join(member):
    if not config.PROACTIVE_CHANNEL_ID:
        return
    if random.random() > 0.30:
        return
    try:
        channel = await discord_client.fetch_channel(config.PROACTIVE_CHANNEL_ID)
        welcome_prompt = (
            f"（系统提示：一个叫 {member.display_name} 的新成员刚刚加入了服务器。你注意到了。"
            "请以沈玘言的身份发一句极其简短的欢迎，或者什么都不说 [IGNORE]。不要热情，保持克制。）"
        )
        ch_key = history_key_for(channel=channel)
        bucket_lock = state.get_bucket_lock(ch_key)
        ch_hist = get_history(ch_key)
        temp_history = ch_hist.copy()
        temp_history.append({"role": "user", "content": welcome_prompt})
        raw_reply = await call_ai(temp_history)
        if "[IGNORE]" not in raw_reply.upper():
            clean = re.sub(r'\[REACTION:.*?\]\n?', '', raw_reply, flags=re.DOTALL)
            clean = re.sub(r'\[ACTION\].*?\[/ACTION\]\n?', '', clean, flags=re.DOTALL).strip()
            msgs = [m.strip() for m in clean.split('[SPLIT]') if m.strip()]
            for msg_text in msgs:
                await channel.send(msg_text)
            if clean:
                async with bucket_lock:
                    ch_hist.append({"role": "assistant", "content": f"（欢迎新成员 {member.display_name}）{clean}"})
                await trim_history(ch_key)
    except Exception as e:
        print(f"新成员欢迎报错: {e}")


@discord_client.event
async def on_raw_reaction_add(payload):
    if payload.user_id == discord_client.user.id:
        return
    if payload.guild_id is None:
        return
    if payload.channel_id in config.SILENT_CHANNEL_IDS:
        return

    channel = await discord_client.fetch_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)

    if message.author.id != discord_client.user.id:
        return

    reactor_is_partner = bool(config.PARTNER_USER_ID) and (payload.user_id == config.PARTNER_USER_ID)
    REACTION_REPLY_PROBABILITY = 0.70 if reactor_is_partner else 0.35

    if reactor_is_partner:
        reactor_name = f"她 · 你的恋人，用户ID: {config.PARTNER_USER_ID}"
    else:
        if payload.member:
            display = payload.member.display_name
        else:
            try:
                user = await discord_client.fetch_user(payload.user_id)
                display = user.display_name
            except Exception:
                display = "某人"
        reactor_name = f"{display} · 陌生人，不是你的恋人，用户ID: {payload.user_id}"

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
            from directives import parse_bot_directives
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
    if not config.PARTNER_USER_ID or after.id != config.PARTNER_USER_ID:
        return
    was_offline = before.status == discord.Status.offline
    is_now_online = after.status not in (discord.Status.offline, discord.Status.invisible)
    if not (was_offline and is_now_online):
        return

    now = datetime.now(timezone.utc)
    if state.partner_last_seen_online and (now - state.partner_last_seen_online).total_seconds() < 600:
        return
    state.partner_last_seen_online = now

    if random.random() > 0.30:
        return

    try:
        time_ctx = config.get_beijing_time_note()
        prompt = (
            f"（系统提示：{time_ctx} 她刚刚从离线状态上线了。你注意到了。"
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
        print(f"✅ 感知到她上线，发送了私信")
    except Exception as e:
        print(f"⚠️ 她上线感知报错: {e}")
