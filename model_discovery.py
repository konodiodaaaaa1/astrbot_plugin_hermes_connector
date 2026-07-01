"""Hermes 可用模型发现。

供 LLM 工具在决定「换用哪个模型」前查询：当前 Hermes 激活的
provider 究竟能用哪些模型、模型 ID 的确切格式是什么。

设计要点：
- 本地模式：直接读 Hermes 自己的 config.yaml（model.base_url / model.provider）
  和 .env（API key），请求 provider 的 OpenAI 兼容 `/v1/models`。
- 只返回模型 ID 列表，绝不回传或记录 API key。
- 拉取失败时返回明确错误，调用方据此退回默认模型。
"""
import asyncio
import json
import logging
import os
import re
import urllib.request
from typing import Optional

logger = logging.getLogger("astrbot")

# Hermes 配置文件默认位置（Windows / *nix）
_HERMES_CONFIG_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), "AppData", "Local", "hermes", "config.yaml"),
    os.path.join(os.path.expanduser("~"), ".config", "hermes", "config.yaml"),
    os.path.join(os.path.expanduser("~"), ".hermes", "config.yaml"),
]
_HERMES_ENV_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), "AppData", "Local", "hermes", ".env"),
    os.path.join(os.path.expanduser("~"), ".config", "hermes", ".env"),
    os.path.join(os.path.expanduser("~"), ".hermes", ".env"),
]

# provider → 优先尝试的 API key 环境变量名（按顺序）
_PROVIDER_KEY_ENV = {
    "openai-api": ["OPENAI_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "custom": ["OPENAI_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY", "OPENAI_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY", "OPENAI_API_KEY"],
    "glm": ["GLM_API_KEY", "ZHIPU_API_KEY", "OPENAI_API_KEY"],
    "zai": ["GLM_API_KEY", "ZHIPU_API_KEY", "OPENAI_API_KEY"],
    "qwen": ["DASHSCOPE_API_KEY", "QWEN_API_KEY", "OPENAI_API_KEY"],
}


class ModelDiscoveryError(Exception):
    """模型发现失败"""
    pass


def _first_existing(paths: list[str]) -> Optional[str]:
    for p in paths:
        if os.path.isfile(p):
            return p
    return None


def _read_model_config() -> dict:
    """从 Hermes config.yaml 提取 model.base_url / model.provider / model.default。

    使用轻量正则解析，避免引入 yaml 依赖（插件运行在 AstrBot venv 中）。
    """
    cfg_path = _first_existing(_HERMES_CONFIG_CANDIDATES)
    result = {"base_url": None, "provider": None, "default": None, "config_path": cfg_path}
    if not cfg_path:
        return result
    in_model = False
    try:
        with open(cfg_path, encoding="utf-8") as f:
            for line in f:
                if re.match(r"^model:\s*$", line):
                    in_model = True
                    continue
                if in_model:
                    # 顶格非空行 = 离开 model 块
                    if re.match(r"^\S", line):
                        break
                    m = re.match(r"\s+base_url:\s*(.+?)\s*$", line)
                    if m:
                        result["base_url"] = m.group(1).strip().strip('"').strip("'")
                    m = re.match(r"\s+provider:\s*(.+?)\s*$", line)
                    if m:
                        result["provider"] = m.group(1).strip().strip('"').strip("'")
                    m = re.match(r"\s+default:\s*(.+?)\s*$", line)
                    if m:
                        result["default"] = m.group(1).strip().strip('"').strip("'")
    except Exception as e:
        logger.warning(f"读取 Hermes config.yaml 失败: {e}")
    return result


def _read_env_keys() -> dict:
    """读取 Hermes .env 中的 KEY=VALUE（仅在内存中使用，不落日志）。"""
    env_path = _first_existing(_HERMES_ENV_CANDIDATES)
    keys: dict[str, str] = {}
    if not env_path:
        return keys
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                keys[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:
        logger.warning(f"读取 Hermes .env 失败: {e}")
    return keys


def _resolve_api_key(provider: Optional[str], base_url: Optional[str], env: dict) -> str:
    """按 provider / base_url 选择合适的 API key。找不到则回退 OPENAI_API_KEY。"""
    prov = (provider or "").strip().lower()
    candidates = _PROVIDER_KEY_ENV.get(prov, [])
    # base_url 含 openrouter 时优先 OPENROUTER_API_KEY
    if base_url and "openrouter" in base_url.lower():
        candidates = ["OPENROUTER_API_KEY"] + candidates
    if "OPENAI_API_KEY" not in candidates:
        candidates = candidates + ["OPENAI_API_KEY"]
    for name in candidates:
        if env.get(name):
            return env[name]
    return ""


def _fetch_models_sync(base_url: str, api_key: str, timeout: int = 15) -> list[str]:
    """同步请求 OpenAI 兼容 /v1/models，返回模型 ID 列表。"""
    url = base_url.rstrip("/")
    if not url.endswith("/models"):
        url = url + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    items = data.get("data", data if isinstance(data, list) else [])
    ids = []
    for m in items:
        if isinstance(m, dict) and m.get("id"):
            ids.append(str(m["id"]))
        elif isinstance(m, str):
            ids.append(m)
    return ids


async def discover_models(timeout: int = 15) -> dict:
    """发现当前 Hermes 激活 provider 的可用模型。

    Returns dict:
        {
          "ok": bool,
          "provider": str | None,
          "base_url": str | None,
          "default": str | None,   # config 里的默认模型
          "models": list[str],
          "error": str | None,
        }
    """
    cfg = _read_model_config()
    base_url = cfg.get("base_url")
    provider = cfg.get("provider")
    default_model = cfg.get("default")

    if not base_url:
        return {
            "ok": False, "provider": provider, "base_url": None,
            "default": default_model, "models": [],
            "error": (
                "Hermes config.yaml 未设置 model.base_url，无法通过 /v1/models 发现模型。"
                "该 provider 可能使用内置端点；请直接使用 Hermes 默认模型，"
                "或在发送时用 provider:model 语法手动指定。"
            ),
        }

    env = _read_env_keys()
    api_key = _resolve_api_key(provider, base_url, env)

    try:
        ids = await asyncio.to_thread(_fetch_models_sync, base_url, api_key, timeout)
        return {
            "ok": True, "provider": provider, "base_url": base_url,
            "default": default_model, "models": ids, "error": None,
        }
    except Exception as e:
        return {
            "ok": False, "provider": provider, "base_url": base_url,
            "default": default_model, "models": [],
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }
