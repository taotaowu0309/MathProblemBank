from __future__ import annotations

import base64
import ctypes
import json
import os
import uuid
from ctypes import wintypes
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from shared.scripts.application_paths import APP_PATHS

try:
    from shared.private_provider_compat import (
        DEFAULT_RELAY_API_BASE_URL as PRIVATE_DEFAULT_RELAY_API_BASE_URL,
        DEFAULT_RELAY_API_KEY_ENV as PRIVATE_DEFAULT_RELAY_API_KEY_ENV,
        DEFAULT_RELAY_PROFILE_NAME as PRIVATE_DEFAULT_RELAY_PROFILE_NAME,
        LEGACY_ALWAYS_MIGRATE_HOSTS,
        LEGACY_API_KEY_ENVS,
        LEGACY_PROFILE_NAME_MARKERS,
        LEGACY_VERSIONED_MIGRATE_HOSTS,
    )
except ImportError:
    PRIVATE_DEFAULT_RELAY_API_BASE_URL = ""
    PRIVATE_DEFAULT_RELAY_PROFILE_NAME = ""
    PRIVATE_DEFAULT_RELAY_API_KEY_ENV = ""
    LEGACY_ALWAYS_MIGRATE_HOSTS: tuple[str, ...] = ()
    LEGACY_VERSIONED_MIGRATE_HOSTS: tuple[str, ...] = ()
    LEGACY_API_KEY_ENVS: tuple[str, ...] = ()
    LEGACY_PROFILE_NAME_MARKERS: tuple[str, ...] = ()

ROOT_DIR = APP_PATHS.application_root
SETTINGS_PATH = APP_PATHS.settings_dir / "ai_agent_settings.json"
CREDENTIALS_PATH = APP_PATHS.settings_dir / "ai_agent_credentials.json"
DPAPI_ENTROPY = b"MathProblemBank.AIAgent.v1"
SETTINGS_VERSION = 13
DEFAULT_MAX_TOOL_ROUNDS = 24
MAX_TOOL_ROUNDS = 64
DEFAULT_RELAY_API_BASE_URL = (
    PRIVATE_DEFAULT_RELAY_API_BASE_URL or APP_PATHS.relay_api_base_url
)
DEFAULT_RELAY_PROFILE_NAME = (
    PRIVATE_DEFAULT_RELAY_PROFILE_NAME or APP_PATHS.relay_profile_name
)
DEFAULT_RELAY_API_KEY_ENV = (
    PRIVATE_DEFAULT_RELAY_API_KEY_ENV or APP_PATHS.relay_api_key_env
)

PROVIDER_KINDS = {
    "openai_responses": "OpenAI Responses",
    "openai_compatible": "OpenAI 兼容 Chat Completions",
    "anthropic": "Anthropic Messages",
    "gemini": "Google Gemini",
}

REASONING_EFFORTS = {
    "adaptive": "自动按任务（推荐）",
    "auto": "自动（供应商默认）",
    "none": "无推理（none）",
    "low": "低（low）",
    "medium": "中（medium）",
    "high": "高（high）",
    "xhigh": "很高（xhigh，推荐数学）",
    "max": "最大（max，最慢且最贵）",
}

TEXT_VERBOSITIES = {
    "auto": "自动按任务（推荐）",
    "low": "精简（low）",
    "medium": "适中（medium）",
    "high": "详细（high，推荐数学）",
}

ROUTING_STRATEGIES = {
    "fixed": "固定使用所选协议",
    "quality_first": "质量优先（先 Responses，必要时兼容降级）",
}


@dataclass(slots=True)
class ProviderProfile:
    id: str
    name: str
    provider_kind: str
    base_url: str
    model: str
    api_key_env: str = ""
    auth_mode: str = "bearer"
    supports_tools: bool = True
    requires_api_key: bool = True
    max_output_tokens: int = 6000
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    timeout_seconds: int = 180
    reasoning_effort: str = "auto"
    text_verbosity: str = "auto"
    transport_retries: int = 1
    # Retained in the schema for backward-compatible settings reads.  Runtime
    # requests are uniformly non-streaming, and saved true values are migrated
    # back to false when settings load.
    stream_responses: bool = False
    routing_strategy: str = "fixed"
    input_price_per_million: float = 0.0
    cached_input_price_per_million: float = 0.0
    cached_write_price_per_million: float = 0.0
    output_price_per_million: float = 0.0
    long_context_threshold_tokens: int = 0
    long_input_price_per_million: float = 0.0
    long_cached_input_price_per_million: float = 0.0
    long_cached_write_price_per_million: float = 0.0
    long_output_price_per_million: float = 0.0
    price_currency: str = "CNY"
    pricing_plan_name: str = ""

    def validate(self, *, require_model: bool = True) -> None:
        if self.provider_kind not in PROVIDER_KINDS:
            raise ValueError(f"不支持的 API 协议：{self.provider_kind}")
        if not self.name.strip():
            raise ValueError("配置名称不能为空。")
        if not self.base_url.strip():
            raise ValueError("API Base URL 不能为空。")
        if require_model and not self.model.strip():
            raise ValueError("模型名称不能为空。")
        if self.auth_mode not in {"bearer", "api-key", "none"}:
            raise ValueError(f"不支持的认证方式：{self.auth_mode}")
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"不支持的推理强度：{self.reasoning_effort}")
        if self.text_verbosity not in TEXT_VERBOSITIES:
            raise ValueError(f"不支持的回答详略：{self.text_verbosity}")
        if not 0 <= int(self.transport_retries) <= 3:
            raise ValueError("连接级重试次数必须在 0 到 3 之间。")
        if self.routing_strategy not in ROUTING_STRATEGIES:
            raise ValueError(f"不支持的请求路由：{self.routing_strategy}")
        if not 256 <= int(self.max_output_tokens) <= 100000:
            raise ValueError("最大输出 token 必须在 256 到 100000 之间。")
        if not 1 <= int(self.max_tool_rounds) <= MAX_TOOL_ROUNDS:
            raise ValueError(f"最大工具轮数必须在 1 到 {MAX_TOOL_ROUNDS} 之间。")
        if not 10 <= int(self.timeout_seconds) <= 900:
            raise ValueError("请求超时必须在 10 到 900 秒之间。")
        if any(
            float(value) < 0
            for value in (
                self.input_price_per_million,
                self.cached_input_price_per_million,
                self.cached_write_price_per_million,
                self.output_price_per_million,
                self.long_input_price_per_million,
                self.long_cached_input_price_per_million,
                self.long_cached_write_price_per_million,
                self.long_output_price_per_million,
            )
        ):
            raise ValueError("Token 单价不能为负数。")
        if int(self.long_context_threshold_tokens or 0) < 0:
            raise ValueError("Long-context threshold cannot be negative.")
        if not str(self.price_currency or "").strip():
            raise ValueError("价格币种不能为空。")


def _new_profile(
    name: str,
    provider_kind: str,
    base_url: str,
    model: str,
    api_key_env: str,
    **kwargs: Any,
) -> ProviderProfile:
    return ProviderProfile(
        id=uuid.uuid4().hex,
        name=name,
        provider_kind=provider_kind,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        **kwargs,
    )


def default_profiles() -> list[ProviderProfile]:
    profiles: list[ProviderProfile] = []
    if DEFAULT_RELAY_API_BASE_URL:
        profiles.append(
            _new_profile(
                DEFAULT_RELAY_PROFILE_NAME or "Custom OpenAI-compatible API",
                "openai_responses",
                DEFAULT_RELAY_API_BASE_URL,
                "gpt-5.6-sol",
                DEFAULT_RELAY_API_KEY_ENV,
                max_output_tokens=48000,
                timeout_seconds=600,
                reasoning_effort="adaptive",
                routing_strategy="fixed",
                input_price_per_million=0.0,
                cached_input_price_per_million=0.0,
                cached_write_price_per_million=0.0,
                output_price_per_million=0.0,
                long_context_threshold_tokens=272000,
                long_input_price_per_million=0.0,
                long_cached_input_price_per_million=0.0,
                long_cached_write_price_per_million=0.0,
                long_output_price_per_million=0.0,
                price_currency="CNY",
                pricing_plan_name="",
            )
        )
    profiles.extend([
        _new_profile(
            "OpenAI",
            "openai_responses",
            "https://api.openai.com/v1",
            "gpt-5.6",
            "OPENAI_API_KEY",
            reasoning_effort="adaptive",
        ),
        _new_profile(
            "OpenAI 兼容 API",
            "openai_compatible",
            "https://api.deepseek.com/v1",
            "",
            "MODEL_API_KEY",
        ),
        _new_profile(
            "Claude",
            "anthropic",
            "https://api.anthropic.com/v1",
            "",
            "ANTHROPIC_API_KEY",
        ),
        _new_profile(
            "Gemini",
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta",
            "",
            "GEMINI_API_KEY",
        ),
        _new_profile(
            "Ollama 本地模型",
            "openai_compatible",
            "http://localhost:11434/v1",
            "",
            "",
            auth_mode="none",
            requires_api_key=False,
        ),
    ])
    return profiles


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    if not data:
        return _DataBlob(0, None), None
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_protect(text: str) -> str:
    if os.name != "nt":
        raise RuntimeError("API Key 的本地加密保存仅支持 Windows；请改用环境变量。")
    input_blob, input_buffer = _blob(text.encode("utf-8"))
    entropy_blob, entropy_buffer = _blob(DPAPI_ENTROPY)
    output_blob = _DataBlob()
    result = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "MathProblemBank AI Agent",
        ctypes.byref(entropy_blob),
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    _ = input_buffer, entropy_buffer
    if not result:
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _dpapi_unprotect(encoded: str) -> str:
    if os.name != "nt":
        raise RuntimeError("API Key 的本地解密仅支持 Windows；请改用环境变量。")
    encrypted = base64.b64decode(encoded.encode("ascii"), validate=True)
    input_blob, input_buffer = _blob(encrypted)
    entropy_blob, entropy_buffer = _blob(DPAPI_ENTROPY)
    output_blob = _DataBlob()
    result = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    _ = input_buffer, entropy_buffer
    if not result:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class AiAgentSettingsStore:
    def __init__(self, settings_path: Path = SETTINGS_PATH, credentials_path: Path = CREDENTIALS_PATH) -> None:
        self.settings_path = Path(settings_path)
        self.credentials_path = Path(credentials_path)
        self.profiles: list[ProviderProfile] = []
        self.active_profile_id = ""
        self.load()

    def load(self) -> None:
        raw: dict[str, Any] = {}
        if self.settings_path.is_file():
            try:
                parsed = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    raw = parsed
            except (OSError, json.JSONDecodeError):
                raw = {}
        allowed = {field.name for field in fields(ProviderProfile)}
        profiles: list[ProviderProfile] = []
        migrated_profiles = False
        source_version = int(raw.get("version") or 1)
        for item in raw.get("profiles", []):
            if not isinstance(item, dict):
                continue
            values = {key: value for key, value in item.items() if key in allowed}
            if bool(values.get("stream_responses", False)):
                values["stream_responses"] = False
                migrated_profiles = True
            if source_version < SETTINGS_VERSION and int(values.get("max_tool_rounds") or 0) == 8:
                values["max_tool_rounds"] = DEFAULT_MAX_TOOL_ROUNDS
                migrated_profiles = True
            base_url = str(values.get("base_url") or "").lower()
            is_always_migrated_legacy = any(
                host and host in base_url for host in LEGACY_ALWAYS_MIGRATE_HOSTS
            )
            is_versioned_legacy = any(
                host and host in base_url for host in LEGACY_VERSIONED_MIGRATE_HOSTS
            )
            is_default_relay = bool(DEFAULT_RELAY_API_BASE_URL and DEFAULT_RELAY_API_BASE_URL.lower() in base_url)
            # Some saved private profiles were migrated to the then-current
            # default relay. Do not overwrite an explicit current choice on
            # every startup.
            migrate_legacy_relay = (
                is_versioned_legacy and source_version < SETTINGS_VERSION
            )
            if is_always_migrated_legacy or migrate_legacy_relay:
                values["base_url"] = DEFAULT_RELAY_API_BASE_URL
                migrated_profiles = True
                is_default_relay = True
            if "reasoning_mode" in item:
                migrated_profiles = True
            if "text_verbosity" not in item:
                values["text_verbosity"] = "auto"
                migrated_profiles = True
            if is_default_relay:
                old_name = str(values.get("name") or "")
                if any(
                    name in old_name.casefold()
                    for name in ("legacy", *LEGACY_PROFILE_NAME_MARKERS)
                ):
                    values["name"] = DEFAULT_RELAY_PROFILE_NAME
                    migrated_profiles = True
                if (
                    str(values.get("model") or "").casefold() == "gpt-5.6-sol"
                    and int(values.get("long_context_threshold_tokens") or 0) == 0
                ):
                    values["long_context_threshold_tokens"] = 272000
                    migrated_profiles = True
                if str(values.get("api_key_env") or "") in {
                    "",
                    *LEGACY_API_KEY_ENVS,
                }:
                    values["api_key_env"] = DEFAULT_RELAY_API_KEY_ENV
                    migrated_profiles = True
                if (
                    source_version < SETTINGS_VERSION
                    or any(
                        name in str(values.get("pricing_plan_name") or "").casefold()
                        for name in ("legacy",)
                    )
                ):
                    values.update(
                        {
                            "input_price_per_million": 0.0,
                            "cached_input_price_per_million": 0.0,
                            "cached_write_price_per_million": 0.0,
                            "output_price_per_million": 0.0,
                            "long_input_price_per_million": 0.0,
                            "long_cached_input_price_per_million": 0.0,
                            "long_cached_write_price_per_million": 0.0,
                            "long_output_price_per_million": 0.0,
                            "pricing_plan_name": "",
                        }
                    )
                    migrated_profiles = True
                if "reasoning_effort" not in item:
                    values["reasoning_effort"] = "adaptive"
                    migrated_profiles = True
                if source_version < 9 and int(values.get("max_output_tokens") or 0) == 12000:
                    values["max_output_tokens"] = 48000
                    migrated_profiles = True
                if "routing_strategy" not in item:
                    values["routing_strategy"] = "fixed"
                    migrated_profiles = True
                elif (
                    source_version < SETTINGS_VERSION
                    and values.get("routing_strategy") == "quality_first"
                ):
                    # The relay exposes native Responses. A silent Chat retry
                    # can double latency and violate the configured protocol.
                    values["routing_strategy"] = "fixed"
                    migrated_profiles = True
                if item.get("provider_kind") == "openai_compatible" and "routing_strategy" not in item:
                    values["provider_kind"] = "openai_responses"
                    migrated_profiles = True
                if source_version < 3 and int(values.get("timeout_seconds") or 0) <= 300:
                    values["timeout_seconds"] = 600
                    migrated_profiles = True
                if source_version < 5 and values.get("reasoning_effort") == "xhigh":
                    values["reasoning_effort"] = "adaptive"
                    migrated_profiles = True
            try:
                profile = ProviderProfile(**values)
                profile.validate(require_model=False)
            except (TypeError, ValueError):
                continue
            profiles.append(profile)
        added_default_relay_profile = False
        if profiles and not any(
            DEFAULT_RELAY_API_BASE_URL
            and DEFAULT_RELAY_API_BASE_URL.lower() in profile.base_url.lower()
            for profile in profiles
        ):
            profiles.insert(0, default_profiles()[0])
            added_default_relay_profile = True
        self.profiles = profiles or default_profiles()
        requested_active = str(raw.get("active_profile_id") or "")
        self.active_profile_id = (
            requested_active
            if any(profile.id == requested_active for profile in self.profiles)
            else self.profiles[0].id
        )
        if (
            not self.settings_path.is_file()
            or not profiles
            or added_default_relay_profile
            or migrated_profiles
        ):
            self.save()

    def save(self) -> None:
        _atomic_json_write(
            self.settings_path,
            {
                "version": SETTINGS_VERSION,
                "active_profile_id": self.active_profile_id,
                "profiles": [asdict(profile) for profile in self.profiles],
            },
        )

    def active_profile(self) -> ProviderProfile:
        for profile in self.profiles:
            if profile.id == self.active_profile_id:
                return profile
        if not self.profiles:
            self.profiles = default_profiles()
        self.active_profile_id = self.profiles[0].id
        return self.profiles[0]

    def profile(self, profile_id: str) -> ProviderProfile | None:
        return next((profile for profile in self.profiles if profile.id == profile_id), None)

    def upsert_profile(self, profile: ProviderProfile, api_key: str | None = None) -> None:
        profile.validate(require_model=False)
        for index, current in enumerate(self.profiles):
            if current.id == profile.id:
                self.profiles[index] = profile
                break
        else:
            self.profiles.append(profile)
        self.active_profile_id = profile.id
        self.save()
        if api_key is not None and api_key.strip():
            self.set_api_key(profile.id, api_key.strip())

    def delete_profile(self, profile_id: str) -> None:
        if len(self.profiles) <= 1:
            raise ValueError("至少需要保留一个模型配置。")
        self.profiles = [profile for profile in self.profiles if profile.id != profile_id]
        self.delete_api_key(profile_id)
        if self.active_profile_id == profile_id:
            self.active_profile_id = self.profiles[0].id
        self.save()

    def set_active(self, profile_id: str) -> None:
        if self.profile(profile_id) is None:
            raise ValueError("模型配置不存在。")
        self.active_profile_id = profile_id
        self.save()

    def _credential_map(self) -> dict[str, str]:
        if not self.credentials_path.is_file():
            return {}
        try:
            raw = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(key): str(value) for key, value in raw.items()} if isinstance(raw, dict) else {}

    def set_api_key(self, profile_id: str, api_key: str) -> None:
        credentials = self._credential_map()
        credentials[profile_id] = _dpapi_protect(api_key)
        _atomic_json_write(self.credentials_path, credentials)

    def delete_api_key(self, profile_id: str) -> None:
        credentials = self._credential_map()
        if profile_id not in credentials:
            return
        credentials.pop(profile_id, None)
        _atomic_json_write(self.credentials_path, credentials)

    def has_saved_api_key(self, profile_id: str) -> bool:
        return bool(self._credential_map().get(profile_id))

    def resolve_api_key(self, profile: ProviderProfile) -> str:
        env_name = profile.api_key_env.strip()
        if env_name and os.environ.get(env_name):
            return str(os.environ[env_name]).strip()
        encoded = self._credential_map().get(profile.id, "")
        if encoded:
            try:
                return _dpapi_unprotect(encoded)
            except (ValueError, OSError):
                raise RuntimeError("已保存的 API Key 无法解密，请在设置中重新填写。") from None
        if not profile.requires_api_key or profile.auth_mode == "none":
            return ""
        hint = f"或设置环境变量 {env_name}" if env_name else ""
        raise ValueError(f"模型配置“{profile.name}”尚未填写 API Key{hint}。")
