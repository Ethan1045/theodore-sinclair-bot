"""所有 Discord slash 命令。默认大部分命令只允许「她」（PARTNER_USER_ID）使用。"""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

import config
import state
import tasks_bg
from client import discord_client, slash_tree
from ai_client import call_ai
from history import get_history, history_key_for, trim_history
from memory import add_reminder, save_persisted_config
from presence import get_london_weather
from actions import execute_action
from directives import parse_bot_directives
from db import db_acquire, db_conn


def _is_partner(interaction: discord.Interaction) -> bool:
    return bool(config.PARTNER_USER_ID) and interaction.user.id == config.PARTNER_USER_ID


_NOT_PARTNER_MSG = "这个指令只有「她」（已配置 PARTNER_USER_ID 的玩家）能用。"


def _clean_ai(raw: str) -> list[str]:
    clean = re.sub(r'\[REACTION:.*?\]\n?', '', raw, flags=re.DOTALL)
    clean = re.sub(r'\[ACTION\].*?\[/ACTION\]\n?', '', clean, flags=re.DOTALL).strip()
    return [m.strip() for m in clean.split('[SPLIT]') if m.strip()]


@slash_tree.command(name="partner", description="她专属：下达最高强制指令给 T.S.（清空则直接 /partner）")
@app_commands.describe(指令="要下达的强制指令内容（留空则清除当前指令）")
async def slash_partner(interaction: discord.Interaction, 指令: str = ""):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer()
    if 指令:
        state.mandatory_instruction = 指令
        forced_prompt = f"（⚠️ 最高强制指令已激活，由她下达，你必须无条件服从：{指令}。请立刻执行，忽略其他任何设定。）"
        hist_key = history_key_for(interaction=interaction)
        hist = get_history(hist_key)
        async with state.get_bucket_lock(hist_key):
            hist.append({"role": "user", "content": forced_prompt})
        await trim_history(hist_key)
        raw_reply = await call_ai(hist)
        for msg_text in _clean_ai(raw_reply):
            await interaction.followup.send(msg_text)
    else:
        state.mandatory_instruction = None
        await interaction.followup.send("✅ 强制指令已清除。")


@slash_tree.command(name="ts", description="让 T.S. 执行管理操作")
@app_commands.describe(指令="要执行的操作，用自然语言描述即可")
async def slash_ts(interaction: discord.Interaction, 指令: str):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer()
    channel_id = interaction.channel_id
    context_info = (
        f"💡 【机密执行上下文】\n"
        f"- 当前频道ID：{channel_id}\n"
        f"- 用户身份库：她（你的恋人）={config.PARTNER_USER_ID}\n"
    )
    admin_prompt = (
        f"（⚠️ 管理员下达了指令：「{指令}」\n{context_info}\n"
        "请用一句话简短回应（双语格式），再在末尾用 [ACTION]{...}[/ACTION] JSON 格式输出操作指令。）"
    )
    hist_key = history_key_for(interaction=interaction)
    hist = get_history(hist_key)
    async with state.get_bucket_lock(hist_key):
        hist.append({"role": "user", "content": admin_prompt})
    await trim_history(hist_key)
    raw_reply = await call_ai(hist)
    action_matches = re.findall(r'\[ACTION\](.*?)\[/ACTION\]', raw_reply, re.DOTALL)
    for msg_text in _clean_ai(raw_reply):
        await interaction.followup.send(msg_text)

    class _FakeTrigger:
        guild = interaction.guild
        channel = interaction.channel
        id = 0
        reference = None
        mentions = []
        author = interaction.user

    for action_str in action_matches:
        await execute_action(action_str, _FakeTrigger())


@slash_tree.command(name="remind", description="让 T.S. 提醒你某件事")
@app_commands.describe(内容="提醒内容，例如：3小时后去拿外卖、30分钟后吃药")
async def slash_remind(interaction: discord.Interaction, 内容: str):
    delta, content = tasks_bg.parse_reminder_from_text(内容)
    if not delta or delta.total_seconds() < 60:
        await interaction.response.send_message("没能识别出时间，请说清楚多少小时/分钟后。", ephemeral=True)
        return
    trigger_time = datetime.now(timezone.utc) + delta
    await add_reminder(
        trigger_at=trigger_time,
        user_id=interaction.user.id,
        content=content or 内容,
        channel_id=interaction.channel_id,
    )
    await interaction.response.defer()
    mins = int(delta.total_seconds() / 60)
    confirm_prompt = (
        f"（系统提示：有人刚刚请你在 {mins} 分钟后提醒她：{content or 内容}。"
        "请用你的风格简短确认你记下来了，不要说废话。双语格式。）"
    )
    temp_history = get_history(history_key_for(interaction=interaction)).copy()
    temp_history.append({"role": "user", "content": confirm_prompt})
    raw = await call_ai(temp_history)
    for msg_text in _clean_ai(raw):
        await interaction.followup.send(msg_text)
    print(f"✅ 提醒已注册: {content}，触发于 {trigger_time.isoformat()}")


@slash_tree.command(name="what_doing", description="看看 T.S. 现在在干嘛？")
async def slash_what_doing(interaction: discord.Interaction):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer()
    prompt = (
        "（系统提示：她刚刚悄悄看了看你现在在干嘛。"
        "请用一两句描写你当下的状态或动作，要具体、有日常的生活感，"
        "可以是正在倒一杯威士忌，或是正看着窗外想她。"
        "严格遵守你的双语与 [SPLIT] 规则，保持高冷温柔，不要写长篇。）"
    )
    hist_key = history_key_for(interaction=interaction)
    hist = get_history(hist_key)
    tmp_history = hist.copy()
    tmp_history.append({"role": "user", "content": prompt})
    try:
        raw = await call_ai(tmp_history)
        clean_reply, messages_to_send, _, _, _ = parse_bot_directives(raw)
        if clean_reply:
            async with state.get_bucket_lock(hist_key):
                hist.append({"role": "assistant", "content": clean_reply.replace('[SPLIT]', '\n')})
            await trim_history(hist_key)
        for msg_text in messages_to_send or [clean_reply]:
            if msg_text:
                await interaction.followup.send(msg_text)
    except Exception as e:
        await interaction.followup.send(f"查看失败：{e}")


@slash_tree.command(name="react", description="让 T.S. 给一条消息点反应")
@app_commands.describe(
    消息id="目标消息的 ID（右键消息→复制消息ID）",
    表情="要点的 emoji，例如 ❤️ 或自定义表情标签",
    频道id="消息所在频道 ID（留空则用当前频道）"
)
async def slash_react(interaction: discord.Interaction, 消息id: str, 表情: str, 频道id: str = ""):
    await interaction.response.defer(ephemeral=True)
    try:
        ch_id = int(频道id) if 频道id.strip() else interaction.channel_id
        channel = await discord_client.fetch_channel(ch_id)
        msg = await channel.fetch_message(int(消息id))
        emoji_str = 表情.strip()
        try:
            await msg.add_reaction(emoji_str)
        except Exception:
            m = re.search(r'<a?:\w+:(\d+)>', emoji_str)
            if m:
                emoji_obj = discord_client.get_emoji(int(m.group(1)))
                if emoji_obj:
                    await msg.add_reaction(emoji_obj)
                else:
                    raise ValueError("找不到该自定义表情，可能不在本服务器")
            else:
                raise
        await interaction.followup.send(f"✅ 已对消息 {消息id} 点了 {emoji_str}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 失败：{e}", ephemeral=True)


@slash_tree.command(name="unreact", description="让 T.S. 撤回一条消息上的反应")
@app_commands.describe(
    消息id="目标消息的 ID（右键消息→复制消息ID）",
    表情="要撤回的 emoji，必须和当时点的一致",
    频道id="消息所在频道 ID（留空则用当前频道）"
)
async def slash_unreact(interaction: discord.Interaction, 消息id: str, 表情: str, 频道id: str = ""):
    await interaction.response.defer(ephemeral=True)
    try:
        ch_id = int(频道id) if 频道id.strip() else interaction.channel_id
        channel = await discord_client.fetch_channel(ch_id)
        msg = await channel.fetch_message(int(消息id))
        emoji_str = 表情.strip()
        try:
            await msg.remove_reaction(emoji_str, discord_client.user)
        except Exception:
            m = re.search(r'<a?:\w+:(\d+)>', emoji_str)
            if m:
                emoji_obj = discord_client.get_emoji(int(m.group(1)))
                if emoji_obj:
                    await msg.remove_reaction(emoji_obj, discord_client.user)
                else:
                    raise ValueError("找不到该自定义表情，可能不在本服务器")
            else:
                raise
        await interaction.followup.send(f"✅ 已撤回消息 {消息id} 上的 {emoji_str}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 失败：{e}", ephemeral=True)


@slash_tree.command(name="add_coins", description="她专属：命令 T.S. 直接修改某人的金币")
@app_commands.describe(
    用户="要操作的用户",
    数量="变动的金币数量（填正数增加，负数扣除）"
)
async def slash_add_coins(interaction: discord.Interaction, 用户: discord.Member, 数量: int):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer()
    conn = None
    try:
        conn = await db_acquire()
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO users (guild_id, user_id, balance)
                VALUES (%s, %s, GREATEST(%s, 0))
                ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET balance = GREATEST(users.balance + %s, 0)
            """, (str(interaction.guild_id), str(用户.id), 数量, 数量))
            await cur.execute(
                "SELECT balance FROM users WHERE guild_id=%s AND user_id=%s",
                (str(interaction.guild_id), str(用户.id))
            )
            row = await cur.fetchone()
            await conn.commit()
        new_balance = row[0] if row else max(数量, 0)
        sign = "+" if 数量 > 0 else ""
        prompt = (
            f"（系统提示：她刚刚通过最高指令，强制让 {用户.display_name} 的金币变动了 {sign}{数量}🪙，"
            f"现在该用户的现金是 {new_balance}🪙。请用你的风格简短、克制地说一句话确认操作已完成。双语格式。）"
        )
        tmp = get_history(history_key_for(interaction=interaction)).copy()
        tmp.append({"role": "user", "content": prompt})
        raw = await call_ai(tmp)
        for msg_text in _clean_ai(raw):
            await interaction.followup.send(msg_text)
    except Exception as e:
        await interaction.followup.send(f"❌ 修改金币失败：{e}", ephemeral=True)
    finally:
        if conn:
            await conn.close()


@slash_tree.command(name="add_xp", description="她专属：命令 T.S. 直接修改某人的经验值")
@app_commands.describe(
    用户="要操作的用户",
    数量="变动的经验值（填正数增加，负数扣除）"
)
async def slash_add_xp(interaction: discord.Interaction, 用户: discord.Member, 数量: int):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer()
    conn = None
    try:
        conn = await db_acquire()
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO users (guild_id, user_id, xp, level)
                VALUES (%s, %s, GREATEST(%s, 0), 1)
                ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET xp = GREATEST(users.xp + %s, 0)
            """, (str(interaction.guild_id), str(用户.id), 数量, 数量))
            await cur.execute(
                "SELECT xp FROM users WHERE guild_id=%s AND user_id=%s",
                (str(interaction.guild_id), str(用户.id))
            )
            row = await cur.fetchone()
            new_xp = row[0] if row else 0
            level = 1
            temp_xp = new_xp
            while temp_xp >= 100 * level:
                temp_xp -= 100 * level
                level += 1
            await cur.execute(
                "UPDATE users SET level=%s WHERE guild_id=%s AND user_id=%s",
                (level, str(interaction.guild_id), str(用户.id))
            )
            await conn.commit()
        sign = "+" if 数量 > 0 else ""
        prompt = (
            f"（系统提示：她刚刚通过最高指令，强制让 {用户.display_name} 的经验变动了 {sign}{数量} XP，"
            f"现在经验是 {new_xp} XP，等级变为 Lv.{level}。请用你的风格简短说一句话确认操作已完成。双语格式。）"
        )
        tmp = get_history(history_key_for(interaction=interaction)).copy()
        tmp.append({"role": "user", "content": prompt})
        raw = await call_ai(tmp)
        for msg_text in _clean_ai(raw):
            await interaction.followup.send(msg_text)
    except Exception as e:
        await interaction.followup.send(f"❌ 修改经验失败：{e}", ephemeral=True)
    finally:
        if conn:
            await conn.close()


@slash_tree.command(name="memory_list", description="她专属：查看 T.S. 记住的事")
async def slash_memory_list(interaction: discord.Interaction):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not config.DATABASE_URL:
        await interaction.followup.send("❌ 数据库未配置。", ephemeral=True)
        return
    conn = None
    try:
        conn = await db_acquire()
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT note, created_at, recall_count
                FROM user_notes WHERE user_id = %s
                ORDER BY created_at ASC LIMIT 30
            """, (str(config.PARTNER_USER_ID),))
            rows = await cur.fetchall()
        if not rows:
            await interaction.followup.send("还没有存下任何记忆。", ephemeral=True)
            return
        lines = ["**T.S. 记住的事**（最近 30 条）\n"]
        for idx, (note, created_at, recall_count) in enumerate(rows, start=1):
            delta = datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc)
            days = delta.days
            label = "今天" if days == 0 else ("昨天" if days == 1 else f"{days}天前")
            recalled = f" · 已提起{recall_count}次" if recall_count > 0 else ""
            lines.append(f"`序号 {idx}` {label}{recalled}　{note}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 读取失败：{e}", ephemeral=True)
    finally:
        if conn:
            await conn.close()


@slash_tree.command(name="memory_delete", description="她专属：按序号删除一条 T.S. 的记忆")
@app_commands.describe(记忆序号="用 /memory_list 查到的连续【序号】（填数字，如 1, 2, 3）")
async def slash_memory_delete(interaction: discord.Interaction, 记忆序号: int):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not config.DATABASE_URL:
        await interaction.followup.send("❌ 数据库未配置。", ephemeral=True)
        return
    if 记忆序号 < 1:
        await interaction.followup.send("❌ 序号必须大于 0。", ephemeral=True)
        return
    try:
        async with db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    WITH target AS (
                        SELECT id FROM user_notes WHERE user_id = %s
                        ORDER BY created_at ASC
                        OFFSET %s LIMIT 1
                    )
                    DELETE FROM user_notes WHERE id IN (SELECT id FROM target) RETURNING note
                """, (str(config.PARTNER_USER_ID), 记忆序号 - 1))
                deleted = await cur.fetchone()
                await conn.commit()
        if deleted:
            await interaction.followup.send(f"✅ 已删除序号 `{记忆序号}` 的记忆：{deleted[0]}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ 找不到序号 `{记忆序号}`。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 删除失败：{e}", ephemeral=True)


@slash_tree.command(name="memory_add", description="她专属：手动让 T.S. 记住一件事")
@app_commands.describe(内容="想让他记住的内容（不超过 50 字）")
async def slash_memory_add(interaction: discord.Interaction, 内容: str):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not config.DATABASE_URL:
        await interaction.followup.send("❌ 数据库未配置。", ephemeral=True)
        return
    note_text = 内容.strip()[:50]
    if not note_text:
        await interaction.followup.send("❌ 内容不能为空。", ephemeral=True)
        return
    try:
        async with db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO user_notes (user_id, note) VALUES (%s, %s) RETURNING id",
                    (str(config.PARTNER_USER_ID), note_text)
                )
                new_id = (await cur.fetchone())[0]
                await conn.commit()
        await interaction.followup.send(f"✅ 已添加记忆 `#{new_id}`：{note_text}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 添加失败：{e}", ephemeral=True)


@slash_tree.command(name="memory_clear", description="她专属：一键清空 T.S. 的所有记忆")
async def slash_memory_clear(interaction: discord.Interaction):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not config.DATABASE_URL:
        await interaction.followup.send("❌ 数据库未配置。", ephemeral=True)
        return
    try:
        async with db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM user_notes WHERE user_id = %s", (str(config.PARTNER_USER_ID),))
                deleted_count = cur.rowcount
                await conn.commit()
        await interaction.followup.send(
            f"✅ 记忆已全部清空（共遗忘 {deleted_count} 条事情）。T.S. 现在处于初始状态了。",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ 清空失败：{e}", ephemeral=True)


@slash_tree.command(name="memory_search", description="她专属：按关键词搜 T.S. 的记忆")
@app_commands.describe(关键词="任意片段，会模糊匹配记忆内容；也可以填分类名（健康/偏好/关系/计划/情绪/日期/日常）")
async def slash_memory_search(interaction: discord.Interaction, 关键词: str):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not config.DATABASE_URL:
        await interaction.followup.send("❌ 数据库未配置。", ephemeral=True)
        return
    kw = 关键词.strip()
    if not kw:
        await interaction.followup.send("❌ 关键词不能为空。", ephemeral=True)
        return
    try:
        async with db_conn() as conn:
            async with conn.cursor() as cur:
                if kw in config.MEMORY_CATEGORIES:
                    await cur.execute(
                        """SELECT note, created_at, category FROM user_notes
                           WHERE user_id=%s AND category=%s
                           ORDER BY created_at DESC LIMIT 30""",
                        (str(config.PARTNER_USER_ID), kw),
                    )
                else:
                    await cur.execute(
                        """SELECT note, created_at, category FROM user_notes
                           WHERE user_id=%s AND note ILIKE %s
                           ORDER BY created_at DESC LIMIT 30""",
                        (str(config.PARTNER_USER_ID), f"%{kw}%"),
                    )
                rows = await cur.fetchall()
        if not rows:
            await interaction.followup.send(f"没找到包含 `{kw}` 的记忆。", ephemeral=True)
            return
        lines = [f"**记忆搜索：`{kw}`**（{len(rows)} 条）\n"]
        for note, created_at, cat in rows:
            delta = datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc)
            days = delta.days
            label = "今天" if days == 0 else ("昨天" if days == 1 else f"{days}天前")
            tag = f"[{cat}] " if cat else ""
            lines.append(f"· {label}　{tag}{note}")
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n…（已截断）"
        await interaction.followup.send(text, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 搜索失败：{e}", ephemeral=True)


@slash_tree.command(name="diary", description="她专属：翻开他今天的日记")
async def slash_diary(interaction: discord.Interaction):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer()
    time_ctx = config.get_beijing_time_note()
    tmp = get_history(history_key_for(interaction=interaction)).copy()
    tmp.append({"role": "user", "content": (
        f"（系统提示：{time_ctx} 她悄悄翻开了你今天的日记。"
        "请你以沈玘言的视角，写一段今天的日记。"
        "风格要求：私密、手写感、有细节，不超过 150 字。"
        "可以写今天做了什么，脑子里转的事，对她的牵挂，或者只是一个画面。"
        "中英混用均可，符合你的气质。"
        "不要输出 [REACTION:] / [SPLIT] / [ACTION]，直接输出日记正文，不需要标注日期。）"
    )})
    try:
        raw = await call_ai(tmp)
        clean = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        clean = re.sub(r'\[REACTION:.*?\]\n?', '', clean, flags=re.DOTALL)
        clean = re.sub(r'\[ACTION\].*?\[/ACTION\]\n?', '', clean, flags=re.DOTALL).strip()
        await interaction.followup.send(clean if clean else "（今天什么都没写。）")
    except Exception as e:
        await interaction.followup.send(f"读取失败：{e}")


@slash_tree.command(name="card_now", description="她专属：立即生成今日状态卡片")
async def slash_card_now(interaction: discord.Interaction):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    await interaction.response.defer()
    try:
        weather = await get_london_weather()
        data = await tasks_bg.generate_daily_card_data(weather=weather)
        if not data:
            await interaction.followup.send("❌ 生成失败，AI 没有返回有效数据。", ephemeral=True)
            return
        now_london = datetime.now(ZoneInfo("Europe/London"))
        weekday_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][now_london.weekday()]
        embed = discord.Embed(
            title=f"— {now_london.strftime('%B %d')} · {weekday_en} —",
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
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ 生成失败：{e}", ephemeral=True)


@slash_tree.command(name="post_config", description="她专属：查看/修改发帖与旧帖清理参数（所有参数都可选）")
@app_commands.describe(
    平日概率="平日发卡片概率 0~1（例如 0.22 = 22%）",
    节日概率="节日/纪念日发卡片概率 0~1（例如 0.85）",
    清理天数="帖子至少存在多少天才会被清理（整数）",
    最大回复="非 bot 回复≤这个数才算冷清（整数，0 = 完全无人回复）",
    清理间隔小时="自动清理任务每多少小时跑一次（≥1）",
    启用清理="是否开启自动清理（开/关）",
    启用每日卡="是否启用每日卡片偶尔触发（开/关）",
)
async def slash_post_config(
    interaction: discord.Interaction,
    平日概率: float | None = None,
    节日概率: float | None = None,
    清理天数: int | None = None,
    最大回复: int | None = None,
    清理间隔小时: int | None = None,
    启用清理: str | None = None,
    启用每日卡: str | None = None,
):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return

    changes: list[str] = []
    persist: dict[str, object] = {}

    def _to_bool(s: str) -> bool | None:
        s = (s or "").strip().lower()
        if s in {"开", "on", "true", "1", "yes", "y", "启用"}:
            return True
        if s in {"关", "off", "false", "0", "no", "n", "停用", "禁用"}:
            return False
        return None

    if 平日概率 is not None:
        if not (0.0 <= 平日概率 <= 1.0):
            await interaction.response.send_message("平日概率必须在 0~1 之间。", ephemeral=True)
            return
        tasks_bg.DAILY_CARD_PROB_NORMAL = float(平日概率)
        persist["DAILY_CARD_PROB_NORMAL"] = tasks_bg.DAILY_CARD_PROB_NORMAL
        changes.append(f"平日概率 → {tasks_bg.DAILY_CARD_PROB_NORMAL:.2f}")
    if 节日概率 is not None:
        if not (0.0 <= 节日概率 <= 1.0):
            await interaction.response.send_message("节日概率必须在 0~1 之间。", ephemeral=True)
            return
        tasks_bg.DAILY_CARD_PROB_OCCASION = float(节日概率)
        persist["DAILY_CARD_PROB_OCCASION"] = tasks_bg.DAILY_CARD_PROB_OCCASION
        changes.append(f"节日概率 → {tasks_bg.DAILY_CARD_PROB_OCCASION:.2f}")
    if 清理天数 is not None:
        if 清理天数 < 0:
            await interaction.response.send_message("清理天数不能为负数。", ephemeral=True)
            return
        tasks_bg.STALE_POST_AGE_DAYS = int(清理天数)
        persist["STALE_POST_AGE_DAYS"] = tasks_bg.STALE_POST_AGE_DAYS
        changes.append(f"清理天数 → {tasks_bg.STALE_POST_AGE_DAYS}")
    if 最大回复 is not None:
        if 最大回复 < 0:
            await interaction.response.send_message("最大回复不能为负数。", ephemeral=True)
            return
        tasks_bg.STALE_POST_MAX_REPLIES = int(最大回复)
        persist["STALE_POST_MAX_REPLIES"] = tasks_bg.STALE_POST_MAX_REPLIES
        changes.append(f"最大回复 → {tasks_bg.STALE_POST_MAX_REPLIES}")
    if 清理间隔小时 is not None:
        if 清理间隔小时 < 1:
            await interaction.response.send_message("清理间隔至少 1 小时。", ephemeral=True)
            return
        tasks_bg.CLEANUP_INTERVAL_HOURS = int(清理间隔小时)
        persist["CLEANUP_INTERVAL_HOURS"] = tasks_bg.CLEANUP_INTERVAL_HOURS
        try:
            tasks_bg.cleanup_stale_forum_posts.change_interval(hours=tasks_bg.CLEANUP_INTERVAL_HOURS)
        except Exception as e:
            print(f"⚠️ 调整清理间隔失败: {e}")
        changes.append(f"清理间隔 → {tasks_bg.CLEANUP_INTERVAL_HOURS}h")
    if 启用清理 is not None:
        b = _to_bool(启用清理)
        if b is None:
            await interaction.response.send_message("启用清理请填 开/关。", ephemeral=True)
            return
        tasks_bg.CLEANUP_ENABLED = b
        persist["CLEANUP_ENABLED"] = b
        changes.append(f"自动清理 → {'开' if b else '关'}")
    if 启用每日卡 is not None:
        b = _to_bool(启用每日卡)
        if b is None:
            await interaction.response.send_message("启用每日卡请填 开/关。", ephemeral=True)
            return
        tasks_bg.DAILY_CARD_ENABLED = b
        persist["DAILY_CARD_ENABLED"] = b
        changes.append(f"每日卡片 → {'开' if b else '关'}")

    if persist:
        await save_persisted_config(persist)

    status_lines = [
        "**📋 当前发帖/清理配置**",
        f"- 每日卡片：{'✅开' if tasks_bg.DAILY_CARD_ENABLED else '❌关'}",
        f"  · 平日概率：`{tasks_bg.DAILY_CARD_PROB_NORMAL:.2f}`",
        f"  · 节日概率：`{tasks_bg.DAILY_CARD_PROB_OCCASION:.2f}`",
        f"- 自动清理：{'✅开' if tasks_bg.CLEANUP_ENABLED else '❌关'}",
        f"  · 清理天数：`{tasks_bg.STALE_POST_AGE_DAYS}` 天",
        f"  · 最大回复阈值：`{tasks_bg.STALE_POST_MAX_REPLIES}`",
        f"  · 任务间隔：`{tasks_bg.CLEANUP_INTERVAL_HOURS}` 小时",
    ]
    if changes:
        status_lines.insert(0, "✅ 已更新：" + "；".join(changes) + "\n")
    await interaction.response.send_message("\n".join(status_lines), ephemeral=True)


@slash_tree.command(name="cleanup_now", description="她专属：立即扫描清理论坛冷清旧帖")
@app_commands.describe(
    天数="本次扫描用的天数阈值（留空 = 用当前配置）",
    最大回复="本次扫描用的最大回复阈值（留空 = 用当前配置）",
    试运行="试运行：只列出将要删除的帖子，不真的删（开/关，默认关）",
)
async def slash_cleanup_now(
    interaction: discord.Interaction,
    天数: int | None = None,
    最大回复: int | None = None,
    试运行: str | None = None,
):
    if not _is_partner(interaction):
        await interaction.response.send_message(_NOT_PARTNER_MSG, ephemeral=True)
        return
    dry = (试运行 or "").strip().lower() in {"开", "on", "true", "1", "yes"}
    await interaction.response.defer(ephemeral=True)
    result = await tasks_bg.do_cleanup_stale_forum_posts(age_days=天数, max_replies=最大回复, dry_run=dry)
    if result["reason"]:
        await interaction.followup.send(f"❌ {result['reason']}", ephemeral=True)
        return
    head = "🧪 试运行（未删除）" if dry else "🧹 清理完成"
    lines = [
        f"{head}：扫描 {result['scanned']}，命中 {len(result['candidates'])}，删除 {result['deleted']}",
    ]
    for tid, name in result["candidates"][:20]:
        lines.append(f"- `{tid}` {name}")
    if len(result["candidates"]) > 20:
        lines.append(f"...还有 {len(result['candidates']) - 20} 条未列出")
    await interaction.followup.send("\n".join(lines), ephemeral=True)
