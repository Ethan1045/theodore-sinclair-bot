"""Discord 管理操作执行器：execute_action 解析并执行 [ACTION]{...}[/ACTION]。"""
import asyncio
import json as _json
import re
import traceback as _tb
from datetime import datetime, timedelta

import discord

import config
import state
import db as _db
from client import discord_client


def classify_discord_error(e: Exception) -> str:
    if isinstance(e, discord.Forbidden):
        return "Forbidden(权限不足/频道不允许/角色层级不够)"
    if isinstance(e, discord.NotFound):
        return "NotFound(对象不存在/消息被删/频道ID不对)"
    if isinstance(e, discord.HTTPException):
        return f"HTTPException(status={getattr(e, 'status', '?')})"
    return e.__class__.__name__


def short_action_context(trigger_message: discord.Message) -> str:
    g = trigger_message.guild
    return (
        f"guild={getattr(g, 'id', None)} "
        f"channel={getattr(trigger_message.channel, 'id', None)} "
        f"trigger_msg={getattr(trigger_message, 'id', None)} "
        f"author={getattr(trigger_message.author, 'id', None)}"
    )


async def execute_action(action_json_str: str, trigger_message: discord.Message):
    def extract_id(raw_val):
        if not raw_val:
            return None
        nums = re.findall(r'\d+', str(raw_val))
        return int(nums[0]) if nums else None

    try:
        def parse_color(raw) -> discord.Color | None:
            if raw is None:
                return None
            if isinstance(raw, int):
                return discord.Color(raw)
            s = str(raw).strip()
            if not s:
                return None
            if s.lower() in {"random", "rand"}:
                return discord.Color.random()
            if s.startswith("#"):
                s = s[1:]
            if s.lower().startswith("0x"):
                s = s[2:]
            if re.fullmatch(r"[0-9a-fA-F]{6}", s):
                return discord.Color(int(s, 16))
            m = re.fullmatch(r"\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*", s)
            if m:
                r, g, b = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return discord.Color.from_rgb(
                    max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))
                )
            return None

        async def safe_add_reaction(msg: discord.Message, emoji_to_add: str):
            emoji_to_add = (emoji_to_add or "").strip()
            if not emoji_to_add or emoji_to_add.upper() == "NONE":
                emoji_to_add = "👀"
            try:
                await msg.add_reaction(emoji_to_add)
                return emoji_to_add
            except Exception:
                m = re.search(r'<a?:\w+:(\d+)>', emoji_to_add)
                if m:
                    eid = int(m.group(1))
                    emoji_obj = discord_client.get_emoji(eid)
                    if emoji_obj:
                        await msg.add_reaction(emoji_obj)
                        return str(emoji_obj)
                raise

        async def safe_remove_reaction(msg: discord.Message, emoji_to_remove: str):
            emoji_to_remove = (emoji_to_remove or "").strip()
            if not emoji_to_remove or emoji_to_remove.upper() == "NONE":
                emoji_to_remove = "👀"
            try:
                await msg.remove_reaction(emoji_to_remove, discord_client.user)
                return emoji_to_remove
            except Exception:
                m = re.search(r'<a?:\w+:(\d+)>', emoji_to_remove)
                if m:
                    eid = int(m.group(1))
                    emoji_obj = discord_client.get_emoji(eid)
                    if emoji_obj:
                        await msg.remove_reaction(emoji_obj, discord_client.user)
                        return str(emoji_obj)
                raise

        async def safe_fetch_message(c_id: int, m_id: int):
            c = await discord_client.fetch_channel(c_id)
            if isinstance(c, discord.Thread):
                try:
                    return await c.fetch_message(m_id)
                except Exception:
                    pass
                try:
                    return await c.fetch_message(c.id)
                except Exception:
                    pass
                async for msg in c.history(limit=1, oldest_first=True):
                    return msg
                raise ValueError("Thread 消息获取失败：thread 内没有可获取的消息")
            if isinstance(c, discord.ForumChannel):
                thread = await discord_client.fetch_channel(m_id)
                if not isinstance(thread, discord.Thread):
                    raise ValueError("论坛频道消息获取失败：message_id 并非 thread_id")
                try:
                    return await thread.fetch_message(m_id)
                except Exception:
                    pass
                try:
                    return await thread.fetch_message(thread.id)
                except Exception:
                    pass
                async for msg in thread.history(limit=1, oldest_first=True):
                    return msg
                raise ValueError("论坛频道消息获取失败：thread 内没有可获取的消息")
            return await c.fetch_message(m_id)

        async def resolve_target_member(guild: discord.Guild, raw_user_id):
            u_id = extract_id(raw_user_id)
            tried_ids: list[int] = []

            async def _fetch(uid: int):
                tried_ids.append(uid)
                return guild.get_member(uid) or await guild.fetch_member(uid)

            if u_id:
                try:
                    return await _fetch(u_id)
                except discord.NotFound:
                    pass

            if getattr(trigger_message, "mentions", None):
                for m in trigger_message.mentions:
                    if not getattr(m, "bot", False):
                        try:
                            return await _fetch(m.id)
                        except discord.NotFound:
                            continue

            if trigger_message.reference and trigger_message.reference.message_id:
                try:
                    ref_msg = await trigger_message.channel.fetch_message(trigger_message.reference.message_id)
                    if ref_msg and ref_msg.author:
                        try:
                            return await _fetch(ref_msg.author.id)
                        except discord.NotFound:
                            pass
                except Exception:
                    pass

            raise ValueError(f"找不到目标成员（尝试过的ID: {tried_ids or '无'}）。请在命令里 @目标用户，或回复目标用户的消息再执行。")

        clean_json_str = action_json_str.strip()
        if clean_json_str.startswith("```json"):
            clean_json_str = clean_json_str[7:]
        elif clean_json_str.startswith("```"):
            clean_json_str = clean_json_str[3:]
        if clean_json_str.endswith("```"):
            clean_json_str = clean_json_str[:-3]
        clean_json_str = clean_json_str.strip()

        data = _json.loads(clean_json_str)
        action_type = data.get("type", "").upper()
        guild = trigger_message.guild

        DESTRUCTIVE_ACTIONS = {
            "KICK", "BAN", "TIMEOUT",
            "ADD_ROLE", "REMOVE_ROLE", "CREATE_ROLE",
            "DELETE_MESSAGE", "DELETE_THREAD",
            "ARCHIVE_THREAD", "LOCK_THREAD", "RENAME_THREAD",
            "PIN_MESSAGE", "UNPIN_MESSAGE",
            "ADD_COINS", "ADD_XP",
            "SEND_DM",
        }
        if action_type in DESTRUCTIVE_ACTIONS:
            requester = getattr(trigger_message, "author", None)
            requester_id = getattr(requester, "id", None)
            requester_is_bot = bool(getattr(requester, "bot", False))
            requester_perms = getattr(requester, "guild_permissions", None)
            is_admin = bool(requester_perms and getattr(requester_perms, "administrator", False))
            is_partner = bool(config.PARTNER_USER_ID) and (requester_id == config.PARTNER_USER_ID)
            if guild is None:
                print(f"🛡️ 拒绝执行 {action_type}：非 guild 上下文。 raw={action_json_str[:200]}")
                return f"⚠️ 操作未执行：{action_type} 不允许在私聊中触发。"
            if requester_is_bot:
                print(f"🛡️ 拒绝执行 {action_type}：发起者是 bot ({requester_id})。")
                return f"⚠️ 操作未执行：{action_type} 不允许由 bot 触发。"
            if action_type in {"ADD_COINS", "ADD_XP"}:
                if not is_partner:
                    print(f"🛡️ 拒绝执行 {action_type}：发起者 {requester_id} 不是 PARTNER。 raw={action_json_str[:200]}")
                    return f"⚠️ 操作未执行：{action_type} 只允许 PARTNER 触发（实际发起者 ID={requester_id}）。"
            else:
                if not (is_partner or is_admin):
                    print(f"🛡️ 拒绝执行 {action_type}：发起者 {requester_id} 既不是 PARTNER 也不是管理员。 raw={action_json_str[:200]}")
                    return f"⚠️ 操作未执行：{action_type} 需要 PARTNER 或管理员发起（实际发起者 ID={requester_id}）。"

        raw_channel_id = data.get("channel_id")
        channel_id = extract_id(raw_channel_id) if extract_id(raw_channel_id) else trigger_message.channel.id

        def get_fallback_msg_id(raw_m_id):
            m_id = extract_id(raw_m_id)
            if m_id:
                return m_id
            if trigger_message.reference:
                return trigger_message.reference.message_id
            if isinstance(trigger_message.channel, discord.Thread):
                return trigger_message.channel.id
            return trigger_message.id

        import random

        if action_type == "CREATE_THREAD":
            channel = await discord_client.fetch_channel(channel_id)
            if isinstance(channel, discord.ForumChannel):
                thread, _ = await channel.create_thread(name=data.get("name", "未命名"), content=data.get("content", "."), auto_archive_duration=1440)
            else:
                thread = await channel.create_thread(name=data.get("name", "未命名"), type=discord.ChannelType.public_thread, auto_archive_duration=1440)
            print(f"✅ 创建子区: {data.get('name')}")

        elif action_type == "CREATE_FORUM_POST":
            channel = await discord_client.fetch_channel(channel_id)
            thread, first_msg = await channel.create_thread(name=data.get("title", "未命名"), content=data.get("content", "."), auto_archive_duration=1440)
            print(f"✅ 发布论坛帖子: {data.get('title')}")
            await asyncio.sleep(1.0)
            _auto_emojis = ["📖", "🖊️", "🩶", "🫡", "📌", "💭"]
            try:
                await first_msg.add_reaction(random.choice(_auto_emojis))
            except Exception as _re:
                print(f"⚠️ 首楼自动反应失败: {_re}")

        elif action_type == "REACT_MESSAGE":
            msg_id = get_fallback_msg_id(data.get("message_id"))
            last_react_err: Exception | None = None
            for _retry in range(3):
                try:
                    msg = await safe_fetch_message(channel_id, msg_id)
                    added = await safe_add_reaction(msg, data.get("emoji", ""))
                    print(f"✅ 添加了反应: {added}")
                    last_react_err = None
                    break
                except Exception as e:
                    last_react_err = e
                    print(f"⚠️ 添加反应失败 (retry {_retry+1}/3, {classify_discord_error(e)}): channel_id={channel_id} message_id={msg_id}")
                    await asyncio.sleep(1.5)
            if last_react_err:
                print(f"❌ 添加反应最终失败 ({classify_discord_error(last_react_err)}): channel_id={channel_id} message_id={msg_id} emoji={data.get('emoji')}")
                raise last_react_err

        elif action_type == "UNREACT_MESSAGE":
            msg_id = get_fallback_msg_id(data.get("message_id"))
            msg = await safe_fetch_message(channel_id, msg_id)
            try:
                removed = await safe_remove_reaction(msg, data.get("emoji", ""))
                print(f"✅ 撤回了反应: {removed}")
            except Exception as e:
                print(f"❌ 撤回反应失败 ({classify_discord_error(e)}): channel_id={channel_id} message_id={msg_id} emoji={data.get('emoji')}")
                raise

        elif action_type in ["ARCHIVE_THREAD", "LOCK_THREAD", "RENAME_THREAD"]:
            t_id = extract_id(data.get("thread_id"))
            if not t_id:
                t_id = trigger_message.channel.id
            thread = await discord_client.fetch_channel(t_id)
            if action_type == "ARCHIVE_THREAD":
                await thread.edit(archived=True)
                print(f"✅ 归档子区")
            elif action_type == "LOCK_THREAD":
                await thread.edit(archived=True, locked=True)
                print(f"✅ 锁定子区")
            elif action_type == "RENAME_THREAD":
                await thread.edit(name=data.get("name", "未命名子区"))
                print(f"✅ 重命名子区: {data.get('name')}")

        elif action_type == "DELETE_THREAD":
            t_id = extract_id(data.get("thread_id"))
            if not t_id:
                t_id = trigger_message.channel.id
            thread = await discord_client.fetch_channel(t_id)
            try:
                await thread.delete()
                print("✅ 删除子区/帖子(thread)成功")
            except Exception as e:
                print(f"❌ 删除子区/帖子失败 ({classify_discord_error(e)}): thread_id={t_id}")
                raise

        elif action_type == "DELETE_MESSAGE":
            msg_id = get_fallback_msg_id(data.get("message_id"))
            msg = await safe_fetch_message(channel_id, msg_id)
            await msg.delete()
            print(f"✅ 删除消息")

        elif action_type == "PIN_MESSAGE":
            msg_id = get_fallback_msg_id(data.get("message_id"))
            msg = await safe_fetch_message(channel_id, msg_id)
            await msg.pin()
            print(f"✅ 置顶消息")

        elif action_type == "UNPIN_MESSAGE":
            msg_id = get_fallback_msg_id(data.get("message_id"))
            msg = await safe_fetch_message(channel_id, msg_id)
            await msg.unpin()
            print(f"✅ 取消置顶")

        elif action_type in ["KICK", "BAN", "TIMEOUT"] and guild:
            u_id = extract_id(data.get("user_id"))
            if not u_id:
                raise ValueError("无法提取到正确的 user_id，AI可能瞎编了")
            member = guild.get_member(u_id) or await guild.fetch_member(u_id)
            if action_type == "KICK":
                await member.kick(reason=data.get("reason", "T.S. executed kick"))
                print(f"✅ 踢出用户")
            elif action_type == "BAN":
                await member.ban(reason=data.get("reason", "T.S. executed ban"), delete_message_days=0)
                print(f"✅ 封禁用户")
            elif action_type == "TIMEOUT":
                until = discord.utils.utcnow() + timedelta(minutes=int(data.get("minutes", 10)))
                await member.timeout(until, reason=data.get("reason", "T.S. executed timeout"))
                print(f"✅ 禁言用户")

        elif action_type in ["ADD_ROLE", "REMOVE_ROLE"] and guild:
            member = await resolve_target_member(guild, data.get("user_id"))
            role_name = data.get("role_name", "")
            role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), guild.roles)
            if not role:
                raise ValueError(f"服务器里没找到叫 '{role_name}' 的身份组")
            if action_type == "ADD_ROLE":
                try:
                    bot_member = guild.get_member(discord_client.user.id)
                    if bot_member and not bot_member.guild_permissions.manage_roles:
                        raise ValueError("Bot 缺少 Manage Roles(管理身份组) 权限")
                    if bot_member and role >= bot_member.top_role:
                        raise ValueError("角色层级不够：目标身份组高于/等于 Bot 的最高身份组")
                    await member.add_roles(role)
                    print(f"✅ 添加身份组: {role_name}")
                except Exception as e:
                    bot_member = guild.get_member(discord_client.user.id)
                    print(
                        f"❌ 添加身份组失败 ({classify_discord_error(e)}): role={role.name}({role.id}) "
                        f"target_user={member.id} bot_top_role={getattr(getattr(bot_member,'top_role',None),'name',None)}"
                    )
                    raise
            elif action_type == "REMOVE_ROLE":
                try:
                    bot_member = guild.get_member(discord_client.user.id)
                    if bot_member and not bot_member.guild_permissions.manage_roles:
                        raise ValueError("Bot 缺少 Manage Roles(管理身份组) 权限")
                    if bot_member and role >= bot_member.top_role:
                        raise ValueError("角色层级不够：目标身份组高于/等于 Bot 的最高身份组")
                    await member.remove_roles(role)
                    print(f"✅ 移除身份组: {role_name}")
                except Exception as e:
                    bot_member = guild.get_member(discord_client.user.id)
                    print(
                        f"❌ 移除身份组失败 ({classify_discord_error(e)}): role={role.name}({role.id}) "
                        f"target_user={member.id} bot_top_role={getattr(getattr(bot_member,'top_role',None),'name',None)}"
                    )
                    raise

        elif action_type == "CREATE_ROLE" and guild:
            try:
                bot_member = guild.get_member(discord_client.user.id)
                if bot_member and not bot_member.guild_permissions.manage_roles:
                    raise ValueError("Bot 缺少 Manage Roles(管理身份组) 权限，无法创建身份组")
                name = (data.get("name") or data.get("role_name") or "").strip()
                if not name:
                    raise ValueError("缺少 name/role_name")
                color = parse_color(data.get("color"))
                hoist = bool(data.get("hoist", False))
                mentionable = bool(data.get("mentionable", False))
                role = await guild.create_role(
                    name=name,
                    color=color or discord.Color.default(),
                    hoist=hoist,
                    mentionable=mentionable,
                    reason="T.S. create role",
                )
                print(f"✅ 创建身份组成功: {role.name} ({role.id})")
            except Exception as e:
                print(f"❌ 创建身份组失败 ({classify_discord_error(e)}): name={data.get('name')} color={data.get('color')}")
                raise

        elif action_type == "SEND_DM":
            u_id = extract_id(data.get("user_id"))
            if not u_id:
                raise ValueError("无法提取到正确的 user_id")
            if u_id not in config.DM_WHITELIST_IDS:
                print(f"🛡️ 拒绝 SEND_DM：目标 {u_id} 不在白名单内。")
                return f"⚠️ 操作未执行：SEND_DM 目标 {u_id} 不在允许范围内。"
            user = await discord_client.fetch_user(u_id)
            await user.send(data.get("content", "."))
            print(f"✅ 私信发送成功")

        elif action_type == "ADD_COINS" and guild:
            u_id = extract_id(data.get("user_id"))
            amount = int(data.get("amount", 0))
            if u_id and amount != 0 and config.DATABASE_URL:
                try:
                    async with _db.db_conn() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute("""
                                INSERT INTO users (guild_id, user_id, balance)
                                VALUES (%s, %s, GREATEST(%s, 0))
                                ON CONFLICT (guild_id, user_id) DO UPDATE
                                    SET balance = GREATEST(users.balance + %s, 0)
                            """, (str(guild.id), str(u_id), amount, amount))
                            await cur.execute(
                                "SELECT balance, bank FROM users WHERE guild_id=%s AND user_id=%s",
                                (str(guild.id), str(u_id))
                            )
                            row = await cur.fetchone()
                            await conn.commit()
                    new_balance = row[0] if row else max(amount, 0)
                    new_bank    = row[1] if row else 0
                    sign = "+" if amount > 0 else ""
                    try:
                        target_member = guild.get_member(u_id) or await guild.fetch_member(u_id)
                        mention = target_member.mention
                    except Exception:
                        mention = f"<@{u_id}>"
                    await trigger_message.channel.send(
                        f"-# 💰 {mention} 金币变动 {sign}{amount}🪙 ｜ 现金 {new_balance}🪙 · 银行 {new_bank}🪙"
                    )
                    print(f"✅ T.S. 操作金币：user={u_id} delta={sign}{amount} 新余额={new_balance}")
                except Exception as e:
                    print(f"❌ 操作金币失败: {e}")
                    raise

        elif action_type == "ADD_XP" and guild:
            u_id = extract_id(data.get("user_id"))
            amount = int(data.get("amount", 0))
            if u_id and amount != 0 and config.DATABASE_URL:
                try:
                    async with _db.db_conn() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute("""
                                INSERT INTO users (guild_id, user_id, xp, level)
                                VALUES (%s, %s, GREATEST(%s, 0), 1)
                                ON CONFLICT (guild_id, user_id) DO UPDATE
                                    SET xp = GREATEST(users.xp + %s, 0)
                            """, (str(guild.id), str(u_id), amount, amount))
                            await cur.execute("SELECT xp FROM users WHERE guild_id=%s AND user_id=%s", (str(guild.id), str(u_id)))
                            row = await cur.fetchone()
                            new_xp = row[0] if row else 0
                            level = 1
                            temp_xp = new_xp
                            while temp_xp >= 100 * level:
                                temp_xp -= 100 * level
                                level += 1
                            await cur.execute(
                                "UPDATE users SET level=%s WHERE guild_id=%s AND user_id=%s",
                                (level, str(guild.id), str(u_id))
                            )
                            await conn.commit()
                    sign = "+" if amount > 0 else ""
                    try:
                        target_member = guild.get_member(u_id) or await guild.fetch_member(u_id)
                        mention = target_member.mention
                    except Exception:
                        mention = f"<@{u_id}>"
                    await trigger_message.channel.send(
                        f"-# 🌟 {mention} 经验变动 {sign}{amount} XP ｜ 当前 {new_xp} XP (Lv.{level})"
                    )
                    print(f"✅ T.S. 操作XP：user={u_id} delta={sign}{amount} 新XP={new_xp} 新Level={level}")
                except Exception as e:
                    print(f"❌ 操作XP失败: {e}")
                    raise

        else:
            print(f"⚠️ 未知操作类型或非服务器环境: {action_type}")

    except Exception as e:
        _tb.print_exc()
        print(
            "❌ 管理操作执行失败\n"
            f"- ctx: {short_action_context(trigger_message)}\n"
            f"- reason: {classify_discord_error(e)} | {e}\n"
            f"- raw: {action_json_str}"
        )
