"""全局配置加载。所有模块通过 get_config() 读取 config.yaml，避免散落硬编码参数。"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@functools.lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_path(relative_path: str) -> Path:
    """把 config.yaml 里的相对路径解析为基于项目根目录的绝对路径。"""
    p = Path(relative_path)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p
