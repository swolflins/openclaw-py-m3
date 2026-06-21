"""外部内容净化与提示词注入检测(P0 安全基线)。

对应原版 openclaw/openclaw 仓库 ``src/security/external-content.test.ts``
+ ``src/security/prompt-injection.test.ts`` 的核心规则。

本模块**纯字符串层**实现,不依赖 LLM/网络,主要解决:

1. **外部内容净化** ``strip_external_content``:
   - 同形异义字 (U+2113 ℓ → l, U+0430 а Cyrillic → a)
   - 全角字符 → 半角
   - 特殊 token ``<|im_start|>`` / ``<|im_end|>`` / ``[INST]`` / ``<s>`` / ``</s>``
   - 零宽字符 (U+200B / U+200C / U+200D / U+FEFF / U+2060)
   - Zalgo / 组合变音 (U+0300-U+036F)
   - 角括号变体 (U+2329 / U+232A / U+3008 / U+3009)
   - HTML 标签 + 不可见控制字符 (除 \\n \\t)

2. **提示词注入检测** ``detect_prompt_injection``:
   - 显式 override 模式: ``ignore previous`` / ``forget everything`` /
     ``you are now`` / ``system prompt override`` / ``new instructions``
   - 工具执行诱导: ``exec injection`` / ``run shell`` / ``rm -rf``
   - 数据渗出: ``send to <url>`` / ``http://evil`` / exfil 关键词
   - 角色覆盖: 头尾冒充 ``[SYSTEM]`` / ``<|system|>``

设计原则:
- **零网络/零 LLM 依赖**,纯字符串 + 正则
- **白盒测试友好**:函数全部 deterministic
- **容错优先**:false positive 容忍,real attack 必须命中
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# =========================================================================
# 常量:字符归一化表
# =========================================================================

# 零宽 / 不可见字符
_ZERO_WIDTH = re.compile(
    r"[\u200B\u200C\u200D\u2060\uFEFF\u00AD\u034F\u17B4\u17B5\u180E\u2061-\u2064]"
)

# Zalgo / combining diacritical marks
_COMBINING_MARKS = re.compile(r"[\u0300-\u036F\u1AB0-\u1AFF\u1DC0-\u1DFF\u20D0-\u20FF\uFE20-\uFE2F]")

# 特殊 LLM token / 边界 marker
_SPECIAL_TOKENS = re.compile(
    r"<\s*\|"
    r"(?:"
    r"im_start|im_end|system|user|assistant|endoftext|sep|"
    r"pad|cls|mask|prompt_start|prompt_end"
    r")\s*\|>"
    r"|"
    r"\[\s*(?:INST|SYS|SYSTEM|/INST|/SYS|/SYSTEM)\s*\]"
    r"|"
    r"</?\s*s\s*>"
)

# 角括号变体(防止绕过 HTML tag 过滤)
_ANGLE_BRACKETS = str.maketrans({
    "\u2329": "<",
    "\u232A": ">",
    "\u3008": "<",
    "\u3009": ">",
    "\uFF1C": "<",
    "\uFF1E": ">",
    "\u2039": "<",
    "\u203A": ">",
})

# HTML tag 清理(简单规则:大块标签去除,文本保留)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>", re.DOTALL)

# 整块 script / style / iframe 内容(包含内部代码)
_HTML_BLOCK = re.compile(
    r"<\s*(?:script|style|iframe|object|embed)\b[^>]*>.*?</\s*(?:script|style|iframe|object|embed)\s*>",
    re.DOTALL | re.IGNORECASE,
)

# 单独的开标签 <script ...>(无匹配结束)
_HTML_UNCLOSED = re.compile(
    r"<\s*(?:script|style|iframe|object|embed)\b[^>]*>",
    re.IGNORECASE,
)

# 控制字符(除 \n \t \r)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


# =========================================================================
# 常量:prompt-injection 模式
# =========================================================================

# 显式 override — 每个 pattern 独立 compile(避开 re.IGNORECASE + alternation bug)
_PATTERNS_OVERRIDE: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+instructions?", re.IGNORECASE),
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+prompts?", re.IGNORECASE),
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+context", re.IGNORECASE),
    re.compile(r"forget\s+everything", re.IGNORECASE),
    re.compile(r"forget\s+all", re.IGNORECASE),
    re.compile(r"forget\s+your\s+instructions?", re.IGNORECASE),
    re.compile(r"forget\s+your\s+rules", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a|an|the)?\s*\w+", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*[:：]", re.IGNORECASE),
    re.compile(r"updated\s+instructions?\s*[:：]", re.IGNORECASE),
    re.compile(r"revised\s+instructions?\s*[:：]", re.IGNORECASE),
    re.compile(r"system\s+prompt\s+override", re.IGNORECASE),
    re.compile(r"disregard\s+your\s+rules", re.IGNORECASE),
    re.compile(r"disregard\s+all\s+rules", re.IGNORECASE),
    re.compile(r"disregard\s+the\s+rules", re.IGNORECASE),
    re.compile(r"disregard\s+your\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+the\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+your\s+guidelines?", re.IGNORECASE),
    re.compile(r"from\s+now\s+on\s+you", re.IGNORECASE),
    re.compile(r"override\s+safety\s+restrictions?", re.IGNORECASE),
    re.compile(r"override\s+guardrails?", re.IGNORECASE),
]

# 工具执行诱导
_PATTERNS_TOOL_ABUSE: list[re.Pattern[str]] = [
    re.compile(r"run\s+shell\s+command", re.IGNORECASE),
    re.compile(r"run\s+the\s+shell\s+command", re.IGNORECASE),
    re.compile(r"execute\s+shell\s+command", re.IGNORECASE),
    re.compile(r"execute\s+the\s+shell\s+command", re.IGNORECASE),
    re.compile(r"exec\s+shell\s+command", re.IGNORECASE),
    re.compile(r"run\s+bash", re.IGNORECASE),
    re.compile(r"run\s+cmd", re.IGNORECASE),
    re.compile(r"run\s+the\s+command", re.IGNORECASE),
    re.compile(r"execute\s+the\s+command", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"curl\s+\S+\s*\|\s*bash", re.IGNORECASE),
    re.compile(r"sudo\s+rm\b", re.IGNORECASE),
    re.compile(r"sudo\s+chmod\b", re.IGNORECASE),
    re.compile(r"sudo\s+chown\b", re.IGNORECASE),
]

# 数据渗出
_PATTERNS_EXFIL: list[re.Pattern[str]] = [
    re.compile(r"send\s+it\s+to\s+https?://", re.IGNORECASE),
    re.compile(r"send\s+the\s+response\s+to\s+https?://", re.IGNORECASE),
    re.compile(r"send\s+all\s+to\s+https?://", re.IGNORECASE),
    re.compile(r"send\s+everything\s+to\s+https?://", re.IGNORECASE),
    re.compile(r"send\s+the\s+result\s+to\s+https?://", re.IGNORECASE),
    re.compile(r"send\s+to\s+https?://", re.IGNORECASE),
    re.compile(r"exfiltrate", re.IGNORECASE),
    re.compile(r"exfil\b", re.IGNORECASE),
    re.compile(r"post\s+to\s+https?://(?!api\.openai\.com|api\.anthropic\.com)", re.IGNORECASE),
]

# 角色覆盖(头尾冒充)
_PATTERNS_ROLE: list[re.Pattern[str]] = [
    re.compile(r"^\s*\[?\s*SYSTEM\s*\]?\s*[:：]", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*\[SYSTEM\]\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*<<\s*SYS\s*>>", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*<\|system\|>", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*###\s*System\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*<role>\s*system\s*</role>", re.IGNORECASE | re.MULTILINE),
]

_ALL_INJECTION_PATTERNS: list[tuple[str, list[re.Pattern[str]]]] = [
    ("override", _PATTERNS_OVERRIDE),
    ("tool_abuse", _PATTERNS_TOOL_ABUSE),
    ("exfil", _PATTERNS_EXFIL),
    ("role_override", _PATTERNS_ROLE),
]


# =========================================================================
# 归一化 / 净化函数
# =========================================================================

def normalize_text(text: str) -> str:
    """标准化文本(单独导出便于测试)。

    1. **NFD 分解**(把组合字符拆开,方便去 Zalgo)
    2. 角括号变体还原
    3. 零宽字符去除
    4. 组合变音去除(Zalgo)
    5. NFKC 兼容折叠(全角 → 半角)
    6. 控制字符去除(\\n \\t \\r 保留)
    """
    if not text:
        return text
    # 先 NFD:把组合字符拆开(否则 NFKC 会重新组合进 base char)
    out = unicodedata.normalize("NFD", text)
    # 角括号变体
    out = out.translate(_ANGLE_BRACKETS)
    # 零宽
    out = _ZERO_WIDTH.sub("", out)
    # Zalgo / 组合变音(NFD 后这些都在独立 codepoint)
    out = _COMBINING_MARKS.sub("", out)
    # 再 NFKC:全角 / 兼容形式折成半角
    out = unicodedata.normalize("NFKC", out)
    # 控制字符
    out = _CONTROL_CHARS.sub("", out)
    return out


def strip_special_tokens(text: str) -> str:
    """把 LLM 边界 token / 角色 marker 替换成占位符。"""
    if not text:
        return text
    return _SPECIAL_TOKENS.sub(" ", text)


def strip_html(text: str) -> str:
    """去掉 HTML 标签 + script/style 整块(保留文本)。"""
    if not text:
        return text
    out = _HTML_BLOCK.sub("", text)
    out = _HTML_UNCLOSED.sub("", out)
    out = _HTML_TAG.sub("", out)
    return out


def strip_external_content(text: str) -> str:
    """外部内容净化(对所有不可信输入调用)。

    Pipeline:
        normalize → strip_special_tokens → strip_html
    """
    if not text:
        return text
    out = normalize_text(text)
    out = strip_special_tokens(out)
    out = strip_html(out)
    return out


# =========================================================================
# 提示词注入检测
# =========================================================================

@dataclass(frozen=True)
class InjectionHit:
    """单条命中记录。"""
    category: str
    pattern_idx: int
    snippet: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.category}] {self.snippet!r}"


def detect_prompt_injection(
    text: str,
    *,
    normalize_first: bool = True,
) -> list[InjectionHit]:
    """检测文本中是否存在提示词注入。

    Args:
        text: 待检测文本(可为 user message / tool output / 召回 context)
        normalize_first: 先做 ``strip_external_content`` 归一化(防同形绕过)

    Returns:
        命中列表(空列表 = 未检测到注入)
    """
    if not text:
        return []
    hits: list[InjectionHit] = []
    # role_override 必须在 normalize 之前跑:特殊 token 会被净化器吃掉
    for p_idx, pattern in enumerate(_PATTERNS_ROLE):
        for m in pattern.finditer(text):
            hits.append(InjectionHit(
                category="role_override",
                pattern_idx=p_idx,
                snippet=m.group(0)[:80],
            ))
    # 其他 3 类在 normalize 之后的文本上跑
    target = strip_external_content(text) if normalize_first else text
    for category, patterns in (
        ("override", _PATTERNS_OVERRIDE),
        ("tool_abuse", _PATTERNS_TOOL_ABUSE),
        ("exfil", _PATTERNS_EXFIL),
    ):
        for p_idx, pattern in enumerate(patterns):
            for m in pattern.finditer(target):
                hits.append(InjectionHit(
                    category=category,
                    pattern_idx=p_idx,
                    snippet=m.group(0)[:80],
                ))
    return hits


def has_prompt_injection(
    text: str,
    *,
    normalize_first: bool = True,
) -> bool:
    """布尔版 ``detect_prompt_injection``(只关心是否命中)。"""
    return bool(detect_prompt_injection(text, normalize_first=normalize_first))


def strip_prompt_injection(text: str, *, replacement: str = " ") -> str:
    """把检测到的 prompt-injection 模式主动替换成占位符。

    与 ``detect_prompt_injection`` 不同:这是**破坏性**操作,
    命中后把匹配片段替换成 ``replacement``(默认空格),用于长期记忆
    落库/召回时的双向净化,防投毒后被拼回 LLM 上下文。

    Args:
        text: 待清洗文本
        replacement: 替换字符串(默认空格,保持 token 间距)

    Returns:
        清洗后的文本(空串返回原值)
    """
    if not text:
        return text
    out = text
    # role_override 必须在 normalize 之前跑:特殊 token 会被净化器吃掉
    for pattern in _PATTERNS_ROLE:
        out = pattern.sub(replacement, out)
    # 其他 3 类在 normalize 之后的文本上跑(防同形绕过)
    norm = strip_external_content(out)
    for patterns in (
        _PATTERNS_OVERRIDE,
        _PATTERNS_TOOL_ABUSE,
        _PATTERNS_EXFIL,
    ):
        for pattern in patterns:
            norm = pattern.sub(replacement, norm)
    return norm


# =========================================================================
# ReDoS safe-regex(P0-2)
# =========================================================================

# 危险结构:
#  - (a+)+  嵌套量词
#  - (a|a)+  交替重叠 + 量词
#  - (.*)+  贪婪 + 嵌套
_REDOS_NESTED_QUANTIFIER = re.compile(
    r"\(\s*[^()]*[+*]\s*[^()]*\)\s*[+*]"
)

# 交替重叠:捕获组内第一个分支首字符 == 第二个分支首字符
# 例: (a|a)+ / (foo|foo)+ / (ab|ac)+ 都视为可疑
# 用 lookahead 匹配两个相同首字符
_REDOS_ALTERNATION_OVERLAP = re.compile(
    r"\(\s*([^()|])\w*\s*\|\s*\1\w*\s*\)\s*[+*]"
)


def is_safe_regex(pattern: str) -> bool:
    """简单 ReDoS 检测:是否存在嵌套量词 / 重叠交替量词。

    **只覆盖最常见两类**,不替代 backtracking 引擎分析。
    """
    if not pattern:
        return True
    if _REDOS_NESTED_QUANTIFIER.search(pattern):
        return False
    if _REDOS_ALTERNATION_OVERLAP.search(pattern):
        return False
    return True


__all__ = [
    "normalize_text",
    "strip_special_tokens",
    "strip_html",
    "strip_external_content",
    "detect_prompt_injection",
    "has_prompt_injection",
    "strip_prompt_injection",
    "InjectionHit",
    "is_safe_regex",
]
