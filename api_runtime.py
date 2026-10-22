"""Encrypted runtime API profiles and the owner-only Discord configuration panel."""
from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse

import discord
import psycopg
from cryptography.fernet import Fernet, InvalidToken
from openai import AsyncOpenAI


@dataclass(slots=True)
class ApiProfile:
    profile_id: int
    name: str
    base_url: str
    model_name: str
    enabled: bool
    priority: int


class ApiRuntime:
    """Routes OpenAI-compatible calls through encrypted, ordered API profiles."""

    def __init__(self, *, scope: str, database_url: str, master_secret: str,
                 timeout: float = 90):
        self.scope = scope
        self.database_url = database_url
        self.timeout = timeout
        material = f"{scope}\0{master_secret}".encode("utf-8")
        self._cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))
        self._profiles: list[ApiProfile] = []
        self._secrets: dict[int, str] = {}
        self._clients: dict[tuple[int, str, str], AsyncOpenAI] = {}
        self._lock = asyncio.Lock()
        self.chat = _ChatProxy(self)

    @staticmethod
    def _normalize_url(value: str) -> str:
        value = (value or "").strip().rstrip("/")
        return value + "/" if value else ""

    @staticmethod
    def validate_url(value: str) -> str:
        normalized = ApiRuntime._normalize_url(value)
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("网址必须是完整的 http:// 或 https:// 地址，且不能在网址里夹带账号密码。")
        if len(normalized) > 500:
            raise ValueError("API 网址过长。")
        return normalized

    def _encrypt(self, value: str) -> str:
        return "fernet:v1:" + self._cipher.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        if not value.startswith("fernet:v1:"):
            raise ValueError("数据库中的 API 密钥不是受支持的加密格式。")
        try:
            return self._cipher.decrypt(value[10:].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("API 密钥无法解密；Discord Token 可能在保存配置后被更换过。") from exc

    async def initialize(self) -> None:
        if not self.database_url:
            return
        async with self._lock:
            async with await psycopg.AsyncConnection.connect(self.database_url) as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        CREATE TABLE IF NOT EXISTS bot_api_profiles (
                            id BIGSERIAL PRIMARY KEY,
                            bot_scope TEXT NOT NULL,
                            name TEXT NOT NULL,
                            base_url TEXT NOT NULL,
                            encrypted_api_key TEXT NOT NULL,
                            model_name TEXT NOT NULL DEFAULT '',
                            enabled BOOLEAN NOT NULL DEFAULT TRUE,
                            priority INTEGER NOT NULL DEFAULT 100,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            UNIQUE (bot_scope, name)
                        )
                    """)
                    await conn.commit()
            await self._reload_unlocked()

    async def reload(self) -> None:
        async with self._lock:
            await self._reload_unlocked()

    async def _reload_unlocked(self) -> None:
        profiles: list[ApiProfile] = []
        secrets: dict[int, str] = {}
        if self.database_url:
            async with await psycopg.AsyncConnection.connect(self.database_url) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """SELECT id, name, base_url, encrypted_api_key, model_name, enabled, priority
                           FROM bot_api_profiles WHERE bot_scope=%s ORDER BY priority, id""",
                        (self.scope,),
                    )
                    rows = await cur.fetchall()
            for row in rows:
                try:
                    secrets[int(row[0])] = self._decrypt(str(row[3]))
                except ValueError as exc:
                    print(f"[WARN] {self.scope} API 配置 {row[1]!r} 无法载入：{exc}")
                    continue
                profiles.append(ApiProfile(
                    profile_id=int(row[0]), name=str(row[1]),
                    base_url=self._normalize_url(str(row[2])), model_name=str(row[4]),
                    enabled=bool(row[5]), priority=int(row[6]),
                ))
        self._profiles = profiles
        self._secrets = secrets
        self._clients.clear()

    def profiles(self) -> list[ApiProfile]:
        """Only encrypted profiles saved by the owner configuration command."""
        return list(self._profiles)

    def get_profile(self, profile_id: int) -> ApiProfile | None:
        return next((item for item in self.profiles() if item.profile_id == profile_id), None)

    def _secret_for(self, profile: ApiProfile) -> str:
        return self._secrets.get(profile.profile_id, "")

    def _client_for(self, profile: ApiProfile) -> AsyncOpenAI:
        secret = self._secret_for(profile)
        if not secret:
            raise RuntimeError(f"API 配置 {profile.name} 没有可用密钥。")
        cache_key = (profile.profile_id, profile.base_url, secret)
        client = self._clients.get(cache_key)
        if client is None:
            client = AsyncOpenAI(api_key=secret, base_url=profile.base_url, timeout=self.timeout)
            self._clients[cache_key] = client
        return client

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        text = str(exc).lower()
        return bool(
            status == 429 or (isinstance(status, int) and status >= 500)
            or isinstance(exc, (asyncio.TimeoutError, ConnectionError))
            or any(token in text for token in (
                "rate limit", "too many requests", "timeout", "timed out",
                "connection error", "connection reset", "overloaded", "temporarily unavailable",
            ))
        )

    async def create_chat_completion(self, **kwargs):
        candidates = [profile for profile in self.profiles() if profile.enabled]
        if not candidates:
            raise RuntimeError("尚未配置可用 API；请由 Bot 所有者运行 /api配置。")
        last_exc: Exception | None = None
        for index, profile in enumerate(candidates):
            try:
                request = dict(kwargs)
                request["model"] = profile.model_name
                if not request["model"]:
                    raise RuntimeError(f"API 配置 {profile.name} 尚未选择模型。")
                return await self._client_for(profile).chat.completions.create(**request)
            except Exception as exc:
                last_exc = exc
                if not self._retryable(exc) or index == len(candidates) - 1:
                    raise
                print(f"[WARN] API {profile.name!r} 暂时不可用（{type(exc).__name__}），切换下一条线路。")
        if last_exc:
            raise last_exc
        raise RuntimeError("没有可用的 API 配置。")

    async def save_profile(self, *, name: str, base_url: str, api_key: str,
                           model_name: str = "", profile_id: int | None = None) -> ApiProfile:
        if not self.database_url:
            raise RuntimeError("未配置 DATABASE_URL，无法安全持久化 API 密钥。")
        name = " ".join(name.strip().split())
        if not name or len(name) > 40:
            raise ValueError("名称需要是 1～40 个字符。")
        base_url = self.validate_url(base_url)
        model_name = model_name.strip()
        if len(model_name) > 200:
            raise ValueError("模型名称过长。")
        async with self._lock:
            async with await psycopg.AsyncConnection.connect(self.database_url) as conn:
                async with conn.cursor() as cur:
                    if profile_id is None:
                        if not api_key.strip():
                            raise ValueError("新增 API 时必须填写密钥。")
                        await cur.execute(
                            "SELECT COALESCE(MAX(priority), -1) + 1 FROM bot_api_profiles WHERE bot_scope=%s",
                            (self.scope,),
                        )
                        priority = int((await cur.fetchone())[0])
                        await cur.execute(
                            """INSERT INTO bot_api_profiles
                               (bot_scope, name, base_url, encrypted_api_key, model_name, priority)
                               VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                            (self.scope, name, base_url, self._encrypt(api_key.strip()), model_name, priority),
                        )
                        saved_id = int((await cur.fetchone())[0])
                    else:
                        existing = self.get_profile(profile_id)
                        if existing is None:
                            raise ValueError("只能编辑已保存的 API。")
                        if api_key.strip():
                            await cur.execute(
                                """UPDATE bot_api_profiles SET name=%s, base_url=%s,
                                   encrypted_api_key=%s, model_name=%s, updated_at=NOW()
                                   WHERE bot_scope=%s AND id=%s""",
                                (name, base_url, self._encrypt(api_key.strip()), model_name, self.scope, profile_id),
                            )
                        else:
                            await cur.execute(
                                """UPDATE bot_api_profiles SET name=%s, base_url=%s,
                                   model_name=%s, updated_at=NOW() WHERE bot_scope=%s AND id=%s""",
                                (name, base_url, model_name, self.scope, profile_id),
                            )
                        saved_id = profile_id
                    await conn.commit()
            await self._reload_unlocked()
        return self.get_profile(saved_id)  # type: ignore[return-value]

    async def set_model(self, profile_id: int, model_name: str) -> None:
        if len(model_name.strip()) > 200 or not model_name.strip():
            raise ValueError("模型名称无效。")
        await self._simple_update(profile_id, "model_name", model_name.strip())

    async def set_enabled(self, profile_id: int, enabled: bool) -> None:
        if not enabled and len([p for p in self._profiles if p.enabled]) <= 1:
            raise ValueError("至少要保留一条已启用的已保存 API。")
        await self._simple_update(profile_id, "enabled", enabled)

    async def _simple_update(self, profile_id: int, column: str, value) -> None:
        if column not in {"model_name", "enabled"}:
            raise ValueError("不支持的配置字段。")
        async with self._lock:
            async with await psycopg.AsyncConnection.connect(self.database_url) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"UPDATE bot_api_profiles SET {column}=%s, updated_at=NOW() WHERE bot_scope=%s AND id=%s",
                        (value, self.scope, profile_id),
                    )
                    if cur.rowcount != 1:
                        raise ValueError("找不到这条 API 配置。")
                    await conn.commit()
            await self._reload_unlocked()

    async def make_primary(self, profile_id: int) -> None:
        async with self._lock:
            async with await psycopg.AsyncConnection.connect(self.database_url) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE bot_api_profiles SET priority=priority+1 WHERE bot_scope=%s",
                        (self.scope,),
                    )
                    await cur.execute(
                        "UPDATE bot_api_profiles SET priority=0, enabled=TRUE, updated_at=NOW() WHERE bot_scope=%s AND id=%s",
                        (self.scope, profile_id),
                    )
                    if cur.rowcount != 1:
                        raise ValueError("找不到这条 API 配置。")
                    await conn.commit()
            await self._reload_unlocked()

    async def fetch_models(self, profile_id: int) -> list[str]:
        profile = self.get_profile(profile_id)
        if profile is None:
            raise ValueError("找不到这条 API 配置。")
        page = await self._client_for(profile).models.list()
        names = sorted({str(item.id).strip() for item in page.data if str(item.id).strip()}, key=str.lower)
        if not names:
            raise RuntimeError("接口返回了空模型列表。")
        return names


class _ChatCompletionsProxy:
    def __init__(self, runtime: ApiRuntime):
        self._runtime = runtime

    async def create(self, **kwargs):
        return await self._runtime.create_chat_completion(**kwargs)


class _ChatProxy:
    def __init__(self, runtime: ApiRuntime):
        self.completions = _ChatCompletionsProxy(runtime)


class ApiConfigPanel:
    def __init__(self, runtime: ApiRuntime, owner_check):
        self.runtime = runtime
        self.owner_check = owner_check

    async def send(self, interaction: discord.Interaction) -> None:
        deferred_here = not interaction.response.is_done()
        if deferred_here:
            await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self.owner_check(interaction):
            await _deny(interaction)
            return
        view = ApiConfigView(self)
        await view.prepare()
        embed = view.build_embed()
        if deferred_here:
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def _deny(interaction: discord.Interaction) -> None:
    message = "❌ 这项配置只允许 Bot 所有者使用。"
    if interaction.response.is_done():
        try:
            await interaction.edit_original_response(content=message, embed=None, view=None)
        except discord.HTTPException:
            await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class ApiConfigView(discord.ui.View):
    def __init__(self, panel: ApiConfigPanel):
        super().__init__(timeout=900)
        self.panel = panel
        self.selected_id: int | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = await self.panel.owner_check(interaction)
        if not allowed:
            await _deny(interaction)
        return allowed

    async def prepare(self) -> None:
        profiles = self.panel.runtime.profiles()
        if self.selected_id is None and profiles:
            self.selected_id = profiles[0].profile_id
        self.clear_items()
        if profiles:
            self.add_item(_ProfileSelect(self, profiles[:25]))
        self.add_item(_AddButton(self))
        self.add_item(_EditButton(self))
        self.add_item(_ModelsButton(self))
        self.add_item(_PrimaryButton(self))
        self.add_item(_ToggleButton(self))
        self.add_item(_RefreshButton(self))

    def selected(self) -> ApiProfile | None:
        return self.panel.runtime.get_profile(self.selected_id or -1)

    def build_embed(self) -> discord.Embed:
        profiles = self.panel.runtime.profiles()
        embed = discord.Embed(title="🔐 API 运行时配置", color=0x5865F2)
        embed.description = (
            "保存后立即生效，无需重启。密钥只会加密入库，不会在 Discord 中回显。\n"
            "主 API 遇到 429、超时、连接错误或上游 5xx 时，会按顺序切换到副 API。"
        )
        if not profiles:
            embed.add_field(name="当前状态", value="尚无可用配置，请点击“添加 API”。", inline=False)
            return embed
        lines = []
        primary_id = next((item.profile_id for item in profiles if item.enabled), None)
        for index, profile in enumerate(profiles, 1):
            marker = "👉 " if profile.profile_id == self.selected_id else ""
            role = "主" if profile.profile_id == primary_id else "副"
            state = "启用" if profile.enabled else "停用"
            source = "数据库"
            lines.append(
                f"{marker}**{role}｜{profile.name}** · {state} · {source}\n"
                f"`{profile.model_name or '未选择模型'}` · `{profile.base_url}`"
            )
        embed.add_field(name="线路（按调用顺序）", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="仅使用 /api配置 保存的线路；未配置或全部停用时不会回退到环境变量。")
        return embed

    async def refresh_message(self, interaction: discord.Interaction) -> None:
        await self.prepare()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class _ProfileSelect(discord.ui.Select):
    def __init__(self, parent: ApiConfigView, profiles: list[ApiProfile]):
        self.parent_view = parent
        options = [discord.SelectOption(
            label=p.name[:100], value=str(p.profile_id),
            description=(p.model_name or "未选择模型")[:100],
            default=p.profile_id == parent.selected_id,
        ) for p in profiles]
        super().__init__(placeholder="选择要管理的 API", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_id = int(self.values[0])
        await self.parent_view.refresh_message(interaction)


class _AddButton(discord.ui.Button):
    def __init__(self, parent: ApiConfigView):
        self.parent_view = parent
        super().__init__(label="添加 API", emoji="➕", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_ProfileModal(self.parent_view))


class _EditButton(discord.ui.Button):
    def __init__(self, parent: ApiConfigView):
        self.parent_view = parent
        profile = parent.selected()
        super().__init__(label="编辑 / 保存", emoji="💾", row=1,
                         disabled=not profile)

    async def callback(self, interaction: discord.Interaction):
        profile = self.parent_view.selected()
        if not profile:
            await interaction.response.send_message("请先选择已保存的 API 配置。", ephemeral=True)
            return
        await interaction.response.send_modal(_ProfileModal(self.parent_view, profile))


class _ModelsButton(discord.ui.Button):
    def __init__(self, parent: ApiConfigView):
        self.parent_view = parent
        profile = parent.selected()
        super().__init__(label="拉取模型", emoji="📥", row=1,
                         disabled=not profile)

    async def callback(self, interaction: discord.Interaction):
        profile = self.parent_view.selected()
        if not profile:
            await interaction.response.send_message("请先选择 API。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            models = await self.parent_view.panel.runtime.fetch_models(profile.profile_id)
            picker = _ModelPicker(self.parent_view.panel, profile.profile_id, models)
            await interaction.followup.send(
                f"从 **{profile.name}** 拉到 {len(models)} 个模型。\n{picker.page_text()}",
                view=picker, ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ 拉取失败（{type(exc).__name__}），请检查接口、密钥和模型权限；错误详情不会回显。", ephemeral=True)


class _PrimaryButton(discord.ui.Button):
    def __init__(self, parent: ApiConfigView):
        self.parent_view = parent
        profile = parent.selected()
        super().__init__(label="设为主 API", emoji="⭐", row=2,
                         disabled=not profile)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.parent_view.panel.runtime.make_primary(self.parent_view.selected_id or -1)
            await self.parent_view.prepare()
            await interaction.edit_original_response(
                embed=self.parent_view.build_embed(), view=self.parent_view,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ 设置失败（{type(exc).__name__}），请刷新配置后重试。", ephemeral=True)


class _ToggleButton(discord.ui.Button):
    def __init__(self, parent: ApiConfigView):
        self.parent_view = parent
        profile = parent.selected()
        label = "停用" if profile and profile.enabled else "启用"
        super().__init__(label=label, emoji="⏯️", row=2,
                         disabled=not profile)

    async def callback(self, interaction: discord.Interaction):
        profile = self.parent_view.selected()
        await interaction.response.defer(ephemeral=True)
        try:
            if not profile:
                raise ValueError("请先选择 API。")
            await self.parent_view.panel.runtime.set_enabled(profile.profile_id, not profile.enabled)
            await self.parent_view.prepare()
            await interaction.edit_original_response(
                embed=self.parent_view.build_embed(), view=self.parent_view,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ 修改失败（{type(exc).__name__}），请保留至少一条启用的线路。", ephemeral=True)


class _RefreshButton(discord.ui.Button):
    def __init__(self, parent: ApiConfigView):
        self.parent_view = parent
        super().__init__(label="刷新", emoji="🔄", row=2)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.parent_view.panel.runtime.reload()
        await self.parent_view.prepare()
        await interaction.edit_original_response(
            embed=self.parent_view.build_embed(), view=self.parent_view,
        )


class _ProfileModal(discord.ui.Modal):
    def __init__(self, parent: ApiConfigView, profile: ApiProfile | None = None):
        super().__init__(title="编辑 API" if profile else "添加 API", timeout=600)
        self.parent_view = parent
        self.profile = profile
        self.name_input = discord.ui.TextInput(label="名称", max_length=40, default=profile.name if profile else "")
        self.url_input = discord.ui.TextInput(label="API 网址", max_length=500, default=profile.base_url if profile else "https://")
        self.key_input = discord.ui.TextInput(
            label="API 密钥（编辑时留空=不更换）", max_length=2000,
            required=profile is None, style=discord.TextStyle.short,
        )
        self.model_input = discord.ui.TextInput(
            label="模型（可留空，之后拉取选择）", max_length=200, required=False,
            default=profile.model_name if profile else "",
        )
        for item in (self.name_input, self.url_input, self.key_input, self.model_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.parent_view.panel.owner_check(interaction):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            saved = await self.parent_view.panel.runtime.save_profile(
                name=str(self.name_input), base_url=str(self.url_input), api_key=str(self.key_input),
                model_name=str(self.model_input), profile_id=self.profile.profile_id if self.profile else None,
            )
            fresh = ApiConfigView(self.parent_view.panel)
            fresh.selected_id = saved.profile_id
            await fresh.prepare()
            await interaction.followup.send("✅ 已加密保存并立即应用。", embed=fresh.build_embed(), view=fresh, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ 保存失败（{type(exc).__name__}），请检查填写内容和数据库；错误详情不会回显。", ephemeral=True)


class _ModelPicker(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(self, panel: ApiConfigPanel, profile_id: int, models: list[str],
                 query: str = "", page: int = 0):
        super().__init__(timeout=600)
        self.panel = panel
        self.profile_id = profile_id
        self.models = models
        self.query = query
        self.filtered = [m for m in models if query.lower() in m.lower()]
        self.page_count = max(1, (len(self.filtered) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(page, self.page_count - 1))
        start = self.page * self.PAGE_SIZE
        choices = self.filtered[start:start + self.PAGE_SIZE]
        if choices:
            self.add_item(_ModelSelect(self, choices))
        self.add_item(_ModelPageButton(self, -1))
        self.add_item(_ModelPageButton(self, 1))
        self.add_item(_ModelSearchButton(self))

    def page_text(self) -> str:
        scope = f"搜索 `{self.query}`" if self.query else "全部模型"
        return f"{scope}：第 {self.page + 1}/{self.page_count} 页，共 {len(self.filtered)} 个。"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = await self.panel.owner_check(interaction)
        if not allowed:
            await _deny(interaction)
        return allowed


class _ModelSelect(discord.ui.Select):
    def __init__(self, parent: _ModelPicker, choices: list[str]):
        self.parent_view = parent
        self.mapping = {str(i): model for i, model in enumerate(choices)}
        super().__init__(
            placeholder="选择模型并保存",
            options=[discord.SelectOption(label=m[:100], value=str(i)) for i, m in enumerate(choices)],
        )

    async def callback(self, interaction: discord.Interaction):
        model = self.mapping[self.values[0]]
        await interaction.response.defer(ephemeral=True)
        try:
            await self.parent_view.panel.runtime.set_model(self.parent_view.profile_id, model)
            await interaction.edit_original_response(
                content=f"✅ 已选择模型：`{model}`，现在立即生效。", view=None,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ 保存模型失败（{type(exc).__name__}），请刷新配置后重试。", ephemeral=True)


class _ModelSearchButton(discord.ui.Button):
    def __init__(self, parent: _ModelPicker):
        self.parent_view = parent
        super().__init__(label="搜索模型", emoji="🔎")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_ModelSearchModal(self.parent_view))


class _ModelPageButton(discord.ui.Button):
    def __init__(self, parent: _ModelPicker, delta: int):
        self.parent_view = parent
        self.delta = delta
        target = parent.page + delta
        super().__init__(
            label="上一页" if delta < 0 else "下一页",
            emoji="◀️" if delta < 0 else "▶️",
            disabled=target < 0 or target >= parent.page_count,
        )

    async def callback(self, interaction: discord.Interaction):
        picker = _ModelPicker(
            self.parent_view.panel, self.parent_view.profile_id,
            self.parent_view.models, self.parent_view.query,
            self.parent_view.page + self.delta,
        )
        await interaction.response.edit_message(content=picker.page_text(), view=picker)


class _ModelSearchModal(discord.ui.Modal, title="搜索模型"):
    query = discord.ui.TextInput(label="名称包含", max_length=100)

    def __init__(self, parent: _ModelPicker):
        super().__init__()
        self.parent_view = parent

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.parent_view.panel.owner_check(interaction):
            await _deny(interaction)
            return
        query = str(self.query).strip()
        matches = [m for m in self.parent_view.models if query.lower() in m.lower()]
        if not matches:
            await interaction.response.send_message("没有找到匹配模型。", ephemeral=True)
            return
        picker = _ModelPicker(self.parent_view.panel, self.parent_view.profile_id, self.parent_view.models, query)
        await interaction.response.send_message(picker.page_text(), view=picker, ephemeral=True)
