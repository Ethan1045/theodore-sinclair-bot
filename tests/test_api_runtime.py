"""Offline runtime routing, encryption and owner-panel regression checks."""
import asyncio
import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cryptography.fernet import Fernet
import pytest

import api_runtime as module
from api_runtime import ApiProfile, ApiRuntime, ApiConfigPanel, ApiConfigView


def runtime():
    return ApiRuntime(scope="unit-test", database_url="", master_secret="synthetic-master")


def profile(number=1, model="test-model", enabled=True):
    return ApiProfile(number, "test-line", "https://example.invalid/v1/", model, enabled, number)


def test_no_environment_bootstrap_even_when_old_variables_exist(monkeypatch):
    for name in ("OPENAI_API_KEY", "API_KEY", "OPENAI_BASE_URL", "BASE_URL", "MODEL_NAME"):
        monkeypatch.setenv(name, "synthetic-unused-value")
    instance = runtime()
    assert instance.profiles() == []
    with pytest.raises(TypeError):
        ApiRuntime(scope="unit-test", database_url="", master_secret="synthetic-master",
                   bootstrap_key="synthetic-unused-value")


def test_existing_cipher_format_and_scope_remain_compatible():
    material = b"unit-test\x00synthetic-master"
    legacy = Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))
    ciphertext = "fernet:v1:" + legacy.encrypt(b"synthetic-api-value").decode("ascii")
    assert runtime()._decrypt(ciphertext) == "synthetic-api-value"
    other = ApiRuntime(scope="other-test", database_url="", master_secret="synthetic-master")
    with pytest.raises(ValueError):
        other._decrypt(ciphertext)


@pytest.mark.asyncio
async def test_empty_configuration_never_constructs_an_sdk_client(monkeypatch):
    sdk = Mock(side_effect=AssertionError("No provider call is permitted"))
    monkeypatch.setattr(module, "AsyncOpenAI", sdk)
    with pytest.raises(RuntimeError, match="/api配置"):
        await runtime().create_chat_completion(messages=[])
    sdk.assert_not_called()


@pytest.mark.asyncio
async def test_model_comes_only_from_selected_profile():
    instance = runtime()
    instance._profiles = [profile()]
    create = AsyncMock(return_value="response")
    instance._client_for = lambda unused: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert await instance.create_chat_completion(model="ignored-caller-model", messages=[]) == "response"
    assert create.await_args.kwargs["model"] == "test-model"


@pytest.mark.asyncio
async def test_blank_saved_model_does_not_fall_back_to_caller():
    instance = runtime()
    instance._profiles = [profile(model="")]
    with pytest.raises(RuntimeError, match="选择模型"):
        await instance.create_chat_completion(model="ignored-caller-model", messages=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_provider_error_fails_over(status):
    instance = runtime()
    instance._profiles = [profile(1), profile(2)]
    failure = RuntimeError("synthetic provider error")
    failure.status_code = status
    primary = AsyncMock(side_effect=failure)
    secondary = AsyncMock(return_value="secondary-response")
    def client(item):
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=primary if item.profile_id == 1 else secondary)))
    instance._client_for = client
    assert await instance.create_chat_completion(messages=[]) == "secondary-response"
    primary.assert_awaited_once()
    secondary.assert_awaited_once()


@pytest.mark.asyncio
async def test_authentication_error_is_not_silently_routed_elsewhere():
    instance = runtime()
    instance._profiles = [profile(1), profile(2)]
    failure = RuntimeError("synthetic auth failure")
    failure.status_code = 401
    create = AsyncMock(side_effect=failure)
    instance._client_for = lambda item: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(RuntimeError):
        await instance.create_chat_completion(messages=[])
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_disabled_profiles_do_not_make_provider_requests():
    instance = runtime()
    instance._profiles = [profile(enabled=False)]
    with pytest.raises(RuntimeError, match="/api配置"):
        await instance.create_chat_completion(messages=[])


@pytest.mark.asyncio
async def test_configuration_denies_non_owner_before_loading_profiles():
    instance = runtime()
    instance.profiles = Mock(side_effect=AssertionError("No profile access"))
    panel = ApiConfigPanel(instance, AsyncMock(return_value=False))
    interaction = SimpleNamespace(
        response=SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
    )
    await panel.send(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    instance.profiles.assert_not_called()


@pytest.mark.asyncio
async def test_panel_has_no_environment_fallback_and_uses_only_saved_profiles():
    instance = runtime()
    instance._profiles = [profile()]
    panel = ApiConfigPanel(instance, AsyncMock(return_value=True))
    view = ApiConfigView(panel)
    await view.prepare()
    embed = view.build_embed()
    assert "环境变量旧配置" not in str(embed.to_dict())
    assert "不会回退到环境变量" in embed.footer.text
    assert view.selected().profile_id == 1


@pytest.mark.asyncio
async def test_missing_database_refuses_plaintext_profile_storage():
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        await runtime().save_profile(name="test", base_url="https://example.invalid/", api_key="synthetic-api-value")


def test_production_call_sites_do_not_reference_removed_settings():
    root = Path(__file__).resolve().parents[1]
    for name in ("ai_client.py", "history.py", "memory.py", "presence.py", "tasks_bg.py"):
        file = root / name
        if file.exists():
            source = file.read_text(encoding="utf-8-sig")
            assert "config.MODEL_NAME" not in source
            assert "config.API_KEY" not in source
            assert "config.BASE_URL" not in source
    source = (root / ("main.py" if (root / "main.py").exists() else "config.py")).read_text(encoding="utf-8-sig")
    assert 'os.getenv("OPENAI_API_KEY"' not in source
    assert 'os.getenv("MODEL_NAME"' not in source
