"""Hermes 可用模型发现（Hub 侧副本）。

Hub 与 Hermes 部署在同一台机器，直接读 Hermes 的 config.yaml
（model.base_url / model.provider）和 .env（API key），请求 provider 的
OpenAI 兼容 /v1/models，返回模型 ID 列表。

只返回模型 ID，绝不回传或记录 API key。
支持环境变量覆盖配置文件位置：
- HERMES_CONFIG_PATH：Hermes config.yaml 绝对路径
- HERMES_ENV_PATH：Hermes .env 绝对路径
"""
import asyncio
import json
import logging
import os
import re
import urllib.request
from typing import Optional

logger = logging.getLogger("hermes_hub")

_HERMES_CONFIG_CANDIDATES = [
    os.environ.get("HERMES_CONFIG_PATH", ""),
    os.path.join(os.path.expanduser("~"), "AppData", "Local", "hermes", "config.yaml"),
    os.path.join(os.path.expanduser("~"), ".config", "hermes", "config.yaml"),
    os.path.join(os.path.expanduser("~"), ".hermes", "config.yaml"),
]
_HERMES_ENV_CANDIDATES = [
    os.environ.get("HERMES_ENV_PATH", ""),
    os.path.join(os.path.expanduser("~"), "AppData", "Local", "hermes", ".env"),
    os.path.join(os.path.expanduser("~"), ".config", "hermes", ".env"),
    os.path.join(os.path.expanduser("~"), ".hermes", ".env"),
]

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


def _first_existing(paths: list[str]) -> Optional[str]:
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def _read_model_config() -> dict:
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
        logger.warning("读取 Hermes config.yaml 失败: %s", e)
    return result


def _read_env_keys() -> dict:
    env_path = _first_existing(_HERMES_ENV_CANDIDATES)
    keys: dict[str, str] = {}
    # 也允许直接从 Hub 进程环境变量读取（部分部署把 key 注入环境）
    for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
                 "GLM_API_KEY", "ZHIPU_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY"):
        if os.environ.get(name):
            keys[name] = os.environ[name]
    if not env_path:
        return keys
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                keys.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception as e:
        logger.warning("读取 Hermes .env 失败: %s", e)
    return keys


def _resolve_api_key(provider: Optional[str], base_url: Optional[str], env: dict) -> str:
    prov = (provider or "").strip().lower()
    candidates = _PROVIDER_KEY_ENV.get(prov, [])
    if base_url and "openrouter" in base_url.lower():
        candidates = ["OPENROUTER_API_KEY"] + candidates
    if "OPENAI_API_KEY" not in candidates:
        candidates = candidates + ["OPENAI_API_KEY"]
    for name in candidates:
        if env.get(name):
            return env[name]
    return ""


def _fetch_models_sync(base_url: str, api_key: str, timeout: int = 15) -> list[str]:
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
                "请直接使用默认模型，或用 provider:model 语法手动指定。"
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
