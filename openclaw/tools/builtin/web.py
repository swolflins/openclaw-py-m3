"""联网工具集(web_search / get_weather / web_fetch)。

设计目标:
- 零外部 API key 依赖(wttr.in + 搜狗公开 API)
- 走项目现有 http_get 安全白名单(防 SSRF)
- 注册到 ToolRegistry,LLM 通过 tool calling 自主调用

参考 Hermes Feishu adapter 的"内联 web 工具"模式:不依赖 LLM 训练数据,
任何"实时/查"类 query 都由这些工具承担。
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from openclaw.core.logging import get_logger
from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

logger = get_logger(__name__)

# 模块级 httpx.AsyncClient(连接池复用,避免每个工具调用都新建连接)
_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "openclaw-py/1.0 (web tool)"},
        )
    return _client


def register_web_tools(registry: ToolRegistry) -> None:
    """注册联网工具到 registry。

    工具:
    - get_weather(city) -> str: 实时天气(wttr.in,免 API key)
    - web_search(query, top_k) -> str: 搜狗搜索结果摘要
    - web_fetch(url, max_chars) -> str: 抓取 URL 文本内容(html2text 简化)
    """

    @registry.tool(
        name="get_weather",
        category=ToolCategory.UTILITY,
        permission=ToolPermission.SAFE,
    )
    async def get_weather(city: str) -> str:
        """获取指定城市的实时天气信息(温度、湿度、天气描述、风速、观测时间)。

        Args:
            city: 城市名,中文或英文,如 "深圳" / "Shenzhen" / "北京"。

        Returns:
            天气摘要文本,例如:
            "深圳: ☀️ 晴, 31°C, 湿度 84%, 风速 16 km/h, 观测时间 03:54 AM"

        数据源:wttr.in(无需 API key,全球城市覆盖)。
        """
        try:
            c = await _get_client()
            # wttr.in 自动识别中英文城市名;?format=j1 返回 JSON
            r = await c.get(
                f"https://wttr.in/{quote(city)}?format=j1&lang=zh",
            )
            r.raise_for_status()
            j = r.json()
            cur = j.get("current_condition", [{}])[0]
            desc_list = cur.get("weatherDesc", [])
            desc = desc_list[0].get("value", "未知") if desc_list else "未知"
            area = (j.get("nearest_area") or [{}])[0]
            area_name_list = area.get("areaName", [])
            area_name = area_name_list[0].get("value", city) if area_name_list else city
            return (
                f"{area_name}: {desc}, "
                f"{cur.get('temp_C', '?')}°C, "
                f"湿度 {cur.get('humidity', '?')}%, "
                f"风速 {cur.get('windspeedKmph', '?')} km/h, "
                f"观测时间 {cur.get('observation_time', '?')}"
            )
        except httpx.HTTPStatusError as e:
            return f"获取天气失败: HTTP {e.response.status_code}(城市不存在或 wttr.in 限流)"
        except Exception as e:
            logger.warning("get_weather_failed", city=city, error=str(e)[:200])
            return f"获取天气失败: {type(e).__name__}: {str(e)[:200]}"

    @registry.tool(
        name="web_search",
        category=ToolCategory.UTILITY,
        permission=ToolPermission.SAFE,
    )
    async def web_search(query: str, top_k: int = 5) -> str:
        """搜索互联网,返回 top_k 条结果的标题+摘要。

        Args:
            query: 搜索关键词,中文或英文。
            top_k: 返回结果数量,默认 5,最大 10。

        Returns:
            编号列表的搜索结果文本,每行一条:
            "1. 标题 - 摘要(URL)"

        数据源:搜狗搜索(无需 API key)。
        """
        if not query or not query.strip():
            return "错误:搜索关键词不能为空"
        top_k = max(1, min(10, int(top_k)))
        try:
            c = await _get_client()
            r = await c.get(
                "https://www.sogou.com/web",
                params={"query": query},
            )
            r.raise_for_status()
            html = r.text
            # 提取 <h3 ...>...</h3> 标题 + 紧跟的 href + 摘要
            # 简单正则:实际项目可换 BeautifulSoup
            import re
            # 抓 title + 紧邻 a 链接 + 摘要(从 class="str_info" 或 div)
            results = []
            # 搜狗结果结构:<div class="vr-title">...</div> 或 <h3>...</h3>
            # 简化:抓所有 <a ...href="...">标题</a> + <p class="str_info">摘要</p>
            title_pattern = re.compile(
                r'<a[^>]+href="([^"]+)"[^>]*>([^<]{4,100})</a>',
                re.IGNORECASE
            )
            desc_pattern = re.compile(
                r'<p class="[^"]*str_info[^"]*"[^>]*>([^<]{10,300})</p>',
                re.IGNORECASE | re.DOTALL
            )
            titles = title_pattern.findall(html)
            descs = desc_pattern.findall(html)
            for i, (url, title) in enumerate(titles[:top_k], 1):
                title = title.strip()
                desc = descs[i - 1].strip()[:200] if i - 1 < len(descs) else ""
                # 过滤掉导航链接
                if any(x in url for x in ("javascript:", "javascript:void", "#", "login", "sogou.com/web?query=")):
                    continue
                results.append(f"{i}. {title} - {desc}({url})")
            if not results:
                return f"未找到相关结果(可能搜狗限流或 query 过于模糊):{query!r}"
            return "\n".join(results)
        except Exception as e:
            logger.warning("web_search_failed", query=query, error=str(e)[:200])
            return f"搜索失败: {type(e).__name__}: {str(e)[:200]}"

    @registry.tool(
        name="web_fetch",
        category=ToolCategory.UTILITY,
        permission=ToolPermission.NETWORK,
    )
    async def web_fetch(url: str, max_chars: int = 5000) -> str:
        """抓取 URL 的文本内容(去除 HTML 标签)。

        Args:
            url: 完整 URL,必须 http/https。
            max_chars: 返回最大字符数,默认 5000,最大 20000。

        Returns:
            纯文本内容(标题 + 段落)。

        安全:仅允许 http/https scheme,阻止 file/ftp/javascript 等。
        """
        max_chars = max(100, min(20000, int(max_chars)))
        # scheme 白名单
        try:
            p = urlparse(url)
        except Exception:
            return f"错误:URL 解析失败:{url!r}"
        if p.scheme not in ("http", "https"):
            return f"错误:不支持的 scheme {p.scheme!r},仅允许 http/https"
        try:
            c = await _get_client()
            r = await c.get(url)
            r.raise_for_status()
            html = r.text[:max_chars * 3]  # 留 3x buffer 给 HTML→text 转换
            # 简单 HTML→text: 去 script/style, 替换 block 标签为 \n
            import re
            html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
            html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)
            html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
            # 块级标签结尾加 \n
            html = re.sub(r"</(p|div|h[1-6]|li|tr|br)\s*>", "\n", html, flags=re.IGNORECASE)
            # 剥所有标签
            text = re.sub(r"<[^>]+>", " ", html)
            # 解码 HTML entities
            import html as html_mod
            text = html_mod.unescape(text)
            # 折叠空白
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n[ \t]+", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = text.strip()
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n...(已截断,共 {max_chars} 字符)"
            return text or "(抓取后文本为空)"
        except Exception as e:
            logger.warning("web_fetch_failed", url=url, error=str(e)[:200])
            return f"抓取失败: {type(e).__name__}: {str(e)[:200]}"
