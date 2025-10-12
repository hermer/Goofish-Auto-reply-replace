# -*- coding: utf-8 -*-
"""
CookieCloud 同步工具

从 CookieCloud 服务拉取并解析 Cookie，返回可直接用于请求头/WS 的 cookies 字符串。
优先调用 /get/:uuid?password=xxx 返回明文；若返回 {encrypted} 则仅记录警告（未包含密码的服务端不解密）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Dict, List, Optional, Any, Tuple

from loguru import logger
import aiohttp


__all__ = ["fetch_cookiecloud_cookie_str", "build_cookie_str_from_cookie_data"]


def _normalize_host(host: str) -> str:
    if not host:
        return ""
    host = host.strip().rstrip("/")
    if not host.startswith("http://") and not host.startswith("https://"):
        host = "http://" + host
    return host


def _extract_cookie_data(payload: Any) -> Optional[Dict[str, List[dict]]]:
    """
    兼容多种返回格式，提取 cookie_data:
    - { cookie_data: {...}, local_storage_data: {...} }
    - { data: { cookie_data: {...}, ... } }
    - 其他：返回 None
    """
    try:
        obj = json.loads(payload) if isinstance(payload, str) else payload
    except Exception:
        return None

    if not isinstance(obj, dict):
        return None

    if "cookie_data" in obj and isinstance(obj["cookie_data"], dict):
        return obj["cookie_data"]

    if "data" in obj and isinstance(obj["data"], dict) and isinstance(obj["data"].get("cookie_data"), dict):
        return obj["data"]["cookie_data"]

    # 有些服务会返回 { encrypted: "..." }（未解密）
    if "encrypted" in obj:
        return None

    return None


def build_cookie_str_from_cookie_data(cookie_data: Dict[str, List[dict]], prefer_domains: Optional[List[str]] = None) -> str:
    """
    将 CookieCloud 的 cookie_data 转为标准 Cookie 字符串（name=value; ...）
    - 同名 key 以“偏好域名”优先，其次后写覆盖前写
    """
    prefer_domains = prefer_domains or [
        ".goofish.com",
        "goofish.com",
        "wss-goofish.dingtalk.com",
        ".alicdn.com",
        ".taobao.com",
    ]

    def domain_priority(domain: str) -> Tuple[int, int]:
        # 高优先级：命中 prefer_domains（越靠前优先级越高）
        for idx, d in enumerate(prefer_domains):
            if d in domain:
                return (0, idx)
        return (1, 999)

    cookies_ordered: List[Tuple[str, List[dict]]] = []
    for domain, items in (cookie_data or {}).items():
        if not isinstance(items, list):
            continue
        cookies_ordered.append((domain or "", items))

    # 根据偏好域名排序
    cookies_ordered.sort(key=lambda t: domain_priority(t[0]))

    kv: Dict[str, str] = {}
    for domain, items in cookies_ordered:
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("name") or it.get("key")
            value = it.get("value")
            if not name or value is None:
                continue
            # 跳过空值
            value_str = str(value)
            if value_str == "":
                continue
            kv[name] = value_str

    # 输出 name=value; name2=value2
    return "; ".join([f"{k}={v}" for k, v in kv.items()])


async def fetch_cookiecloud_cookie_str(host: str, uuid: str, password: str, timeout: int = 15) -> Optional[str]:
    """
    拉取 CookieCloud 明文并构造 Cookie 字符串。
    返回:
        cookies_str 或 None
    """
    host = _normalize_host(host)
    if not host or not uuid:
        logger.warning("CookieCloud 配置不完整（host/uuid 缺失）")
        return None

    url = f"{host}/get/{uuid}"
    params = {}
    if password:
        params["password"] = password

    try:
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(url, params=params) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error(f"CookieCloud 请求失败: HTTP {resp.status}, 响应: {text[:200]}")
                    return None

                # 优先解析 JSON
                try:
                    data = json.loads(text)
                except Exception:
                    logger.error("CookieCloud 响应不是 JSON")
                    return None

                # 明文返回（推荐）
                cookie_data = _extract_cookie_data(data)
                if cookie_data:
                    cookies_str = build_cookie_str_from_cookie_data(cookie_data)
                    logger.info(f"CookieCloud 拉取成功，合并后 Cookie 长度: {len(cookies_str)}")
                    return cookies_str

                # 未能提取到 cookie_data，可能服务端未启用 password 解密
                if isinstance(data, dict) and "encrypted" in data:
                    logger.warning("CookieCloud 返回了加密字段 encrypted，但服务端未解密。请确保传入 password 或升级服务端。")
                    return None

                logger.error(f"CookieCloud 返回格式无法识别: {str(data)[:200]}")
                return None
    except asyncio.TimeoutError:
        logger.error("CookieCloud 请求超时")
        return None
    except Exception as e:
        logger.error(f"CookieCloud 拉取异常: {e}")
        return None


# 可选：同步封装，便于在同步环境测试
def fetch_cookiecloud_cookie_str_sync(host: str, uuid: str, password: str, timeout: int = 15) -> Optional[str]:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 嵌入到已运行的事件循环中由调用方处理
            logger.error("当前存在运行中的事件循环，无法直接同步调用 fetch_cookiecloud_cookie_str")
            return None
        return loop.run_until_complete(fetch_cookiecloud_cookie_str(host, uuid, password, timeout=timeout))
    except RuntimeError:
        return asyncio.run(fetch_cookiecloud_cookie_str(host, uuid, password, timeout=timeout))