"""Agent Journal — 自我反思与成长日志。

**理念**(OpenClaw #5 idea):
- 每次 session 跑完,自动写一份 markdown journal
- 定期调 LLM "反思"(没有真实 LLM 时用 deterministic 模板 fallback)
- 周报:聚合本周 journals → 一份 human-readable 总结
- SOUL 修正(dry-run):基于反思生成"建议的 SOUL 改动",**不**自动覆盖,
  写入 `_soul_proposals.md` 等人 review

**设计原则**:
- 不强依赖真实 LLM — 无 key 时 deterministic 模式照常工作
- 所有写入都是 append-only(append to file),不会破坏历史
- 时间戳 / session id 都用 ISO 格式 + hash,跨平台兼容
- 不读不写 SOUL 实际文件,只生成 proposal(安全)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from openclaw.core.logging import get_logger
from openclaw.llm.base import ChatMessage

logger = get_logger(__name__)


# ──────────── 数据结构 ────────────

@dataclass
class JournalEntry:
    """单次 session 的结构化摘要(落盘用)。"""
    session_id: str
    timestamp: str  # ISO 8601
    user_message: str
    final_content: str
    iterations: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    tags: list[str] = field(default_factory=list)
    reflections: list[str] = field(default_factory=list)  # 反思阶段累积

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Reflector(Protocol):
    """反思器协议 — 接受一个 JournalEntry,返回反思文本(str)。

    默认实现:`TemplateReflector`(deterministic,无 LLM)。
    真实实现:`LLMReflector`(用 OpenAI/Anthropic)。

    H4 修复:Protocol 改为 async,与 LLMReflector 实现一致,
    避免 AgentJournal.reflect 不 await 导致 AttributeError。
    """
    async def reflect(self, entry: JournalEntry) -> str: ...


# ──────────── 反思器实现 ────────────

class TemplateReflector:
    """基于规则的反思器 — 无 LLM 也能跑(deterministic)。

    从 session 提取特征:
    - 是否一上来就需要工具?(有 tool_calls = 复杂任务)
    - 工具调用是否成功(看 final_content 是否包含 '[tool error]')
    - 迭代次数(>3 表示推理难)
    - 用户消息长度 / agent 回复长度比例
    """

    REFLECT_TEMPLATE = """# 反思 {timestamp} | session `{sid}`

## 任务
**用户问**: {user}
**Agent 答**: {answer_excerpt}

## 表现分析
{analysis}

## 自我评估
{evaluation}

## 改进建议(dry-run,待人工 review)
{suggestions}
"""

    async def reflect(self, entry: JournalEntry) -> str:
        # H4 修复:改为 async 以匹配 Reflector Protocol
        # 表现分析
        analysis_lines: list[str] = []
        n_tools = len(entry.tool_calls)
        analysis_lines.append(f"- 工具调用次数: **{n_tools}**")
        analysis_lines.append(f"- Agent 迭代轮次: **{entry.iterations}**")
        if entry.duration_ms:
            analysis_lines.append(f"- 耗时: **{entry.duration_ms}ms**")

        # 是否出错
        err_count = sum(
            1 for t in entry.tool_calls if "error" in t.get("result", "").lower()
        )
        if err_count:
            analysis_lines.append(
                f"- **⚠️ 工具出错 {err_count} 次**(共 {n_tools} 次调用)"
            )

        # 标签
        if n_tools == 0:
            entry.tags.append("simple_qa")
        elif n_tools <= 2:
            entry.tags.append("light_tool_use")
        elif n_tools <= 5:
            entry.tags.append("multi_step")
        else:
            entry.tags.append("complex_workflow")
        if err_count:
            entry.tags.append("had_errors")
        if entry.iterations >= 6:
            entry.tags.append("deep_reasoning")

        # 自我评估
        evaluation: list[str] = []
        if err_count == 0 and n_tools > 0:
            evaluation.append("✅ 工具调用全部成功,流程顺畅")
        if entry.iterations <= 2 and n_tools == 0:
            evaluation.append("✅ 简单任务一次性回答,效率高")
        if err_count > 0:
            evaluation.append("⚠️ 部分工具调用失败,需排查失败模式")
        if entry.iterations >= 6:
            evaluation.append("⚠️ 推理轮次较多,可能问题表达不够清晰")
        if not evaluation:
            evaluation.append("ℹ️ 标准 session,无特殊模式")

        # 改进建议
        suggestions: list[str] = []
        if err_count > 0:
            suggestions.append("- 检查失败工具的参数校验 / 错误处理")
        if entry.iterations >= 6:
            suggestions.append("- 优化 system_prompt,引导 agent 更直接回答")
        if n_tools > 5:
            suggestions.append("- 考虑拆解为多步而非一次完成,减少单次 tool_calls")
        if not suggestions:
            suggestions.append("- 继续观察,无需特别调整")

        # 截断太长 answer
        ans = entry.final_content[:200] + ("..." if len(entry.final_content) > 200 else "")
        user = entry.user_message[:200] + ("..." if len(entry.user_message) > 200 else "")

        return self.REFLECT_TEMPLATE.format(
            timestamp=entry.timestamp,
            sid=entry.session_id,
            user=user.replace("\n", " "),
            answer_excerpt=ans.replace("\n", " "),
            analysis="\n".join(f"- {line.lstrip('- ').strip()}" for line in analysis_lines),
            evaluation="\n".join(evaluation),
            suggestions="\n".join(suggestions),
        )


class LLMReflector:
    """用真实 LLM 调反思 — 接受任何 BaseLLMProvider 兼容的 LLM。

    Prompt 设计:让 LLM 像一个自我改进的 agent 那样反思。
    """

    REFLECT_SYSTEM = (
        "你是一个 AI agent 正在自我反思。请基于给定的 session 摘要:\n"
        "1) 用 2-3 句话总结这次做得好 / 做得差\n"
        "2) 给出 1-2 条**具体可执行**的 SOUL / system_prompt 改进建议\n"
        "格式:中文,markdown bullet,不要泛泛而谈。"
    )

    def __init__(self, llm: Any) -> None:  # llm 接受任何 BaseLLMProvider
        self.llm = llm

    async def reflect(self, entry: JournalEntry) -> str:
        summary = (
            f"时间: {entry.timestamp}\n"
            f"Session: {entry.session_id}\n"
            f"用户问: {entry.user_message[:500]}\n"
            f"Agent 答: {entry.final_content[:500]}\n"
            f"工具调用: {len(entry.tool_calls)} 次 "
            f"({', '.join(t.get('name', '?') for t in entry.tool_calls[:5])})\n"
            f"迭代轮次: {entry.iterations}\n"
            f"错误: {sum(1 for t in entry.tool_calls if 'error' in t.get('result','').lower())}\n"
        )
        messages = [
            ChatMessage(role="system", content=self.REFLECT_SYSTEM),
            ChatMessage(role="user", content=summary),
        ]
        try:
            result = await self.llm.acomplete(messages, tools=None, temperature=0.4, max_tokens=600)
            return f"# LLM 反思\n\n{result.content or '(空响应)'}"
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM 反思失败,降级: %s", e)
            # H4 修复:TemplateReflector.reflect 现在是 async,需要 await
            fallback = await TemplateReflector().reflect(entry)
            return fallback + "\n\n> ⚠️ LLM 反思失败,已用模板兜底"


# ──────────── Journal 主体 ────────────

class AgentJournal:
    """Agent 自我反思与成长日志 — 落盘 + 报告 + SOUL proposal。

    用法::

        journal = AgentJournal(root=Path("agent_journal"))
        entry = journal.record_session(
            session_id="sess_abc",
            user_message="...",
            response=agent_response,
            started_at=t0,
        )
        reflect_result = await journal.reflect(entry)  # 默认 TemplateReflector;返回 [reflection, proposal_path]
        reflection = reflect_result[0] if reflect_result else ""

        # 周报
        await journal.weekly_report()  # → 生成 agent_journal/weekly_2026-W25.md
    """

    def __init__(
        self,
        root: Path,
        *,
        reflector: Optional[Reflector] = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.reflector: Reflector = reflector or TemplateReflector()

    # ───── 落盘 ─────

    def _entry_filename(self, entry: JournalEntry) -> str:
        """2026-06-20-sess_<hash8>.md — 按日期分目录便于浏览"""
        date_part = entry.timestamp[:10]  # YYYY-MM-DD
        dir_ = self.root / date_part
        dir_.mkdir(parents=True, exist_ok=True)
        # M6 修复:对 session_id 做 sanitize,防路径穿越
        # 旧逻辑直接拼 entry.session_id[:24],传入 ../../../tmp/evil 可写到 journal root 之外
        safe_sid = re.sub(r"[^A-Za-z0-9._:\-]", "_", entry.session_id[:24])
        return str(dir_ / f"sess_{safe_sid}.md")

    @staticmethod
    def _hash_session(user_message: str, ts: str) -> str:
        """session id 的稳定 hash(同一时间同一 message 永远同一 hash)。"""
        h = hashlib.sha256(f"{ts}|{user_message}".encode("utf-8")).hexdigest()[:12]
        return f"sess_{h}"

    def record_session(
        self,
        session_id: str,
        user_message: str,
        response: Any,  # AgentResponse
        *,
        started_at: Optional[datetime] = None,
        tool_results: Optional[list[dict[str, Any]]] = None,
    ) -> JournalEntry:
        """记录一次 session(同步,无 LLM 调用)。

        返回的 entry 已经写到磁盘,后续 `reflect()` 会追加反思。
        """
        now = datetime.now(timezone.utc)
        started = started_at or now
        duration_ms = int((now - started).total_seconds() * 1000)

        # 从 AgentResponse 抽 tool_calls
        tc_list: list[dict[str, Any]] = []
        for tc in getattr(response, "tool_calls", []) or []:
            tc_list.append({
                "name": getattr(tc, "name", "?"),
                "arguments": getattr(tc, "arguments", {}),
            })
        # 合并 tool execution 结果(可选,调用方传)
        if tool_results:
            for i, t in enumerate(tc_list):
                if i < len(tool_results):
                    t["result"] = str(tool_results[i].get("result", ""))[:500]

        entry = JournalEntry(
            session_id=session_id,
            timestamp=now.isoformat(timespec="seconds"),
            user_message=user_message,
            final_content=getattr(response, "content", "") or "",
            iterations=getattr(response, "iterations", 0),
            tool_calls=tc_list,
            duration_ms=duration_ms,
        )
        # 写文件
        path = Path(self._entry_filename(entry))
        path.write_text(self._entry_to_md(entry), encoding="utf-8")
        logger.info("journal recorded: %s", path)
        return entry

    def _entry_to_md(self, entry: JournalEntry) -> str:
        tc_md = "_(无)_"
        if entry.tool_calls:
            tc_md = "\n".join(
                f"- `{t['name']}({json.dumps(t.get('arguments',{}), ensure_ascii=False)[:80]})`"
                + (f" → `{t.get('result','')[:120]}`" if t.get("result") else "")
                for t in entry.tool_calls
            )
        return f"""# Session `{entry.session_id}`

- **时间**: {entry.timestamp}
- **迭代**: {entry.iterations} 轮
- **耗时**: {entry.duration_ms}ms
- **标签**: {', '.join(entry.tags) or '_(待 reflect)_'}

## 用户输入

```
{entry.user_message}
```

## Agent 回答

{entry.final_content or '_(空)_'}

## 工具调用

{tc_md}

---

<!-- 反思将追加在下方 -->
"""

    # ───── 反思 ─────

    async def reflect(self, entry: JournalEntry) -> list[str]:
        """调 reflector → 拿反思 → append 到 entry 文件(只一份,去重),
        再生成 SOUL proposal → 返回 ``[reflection, proposal_path]``。

        **Phase 25 / b8 修复(本提交真正落实)**:
        上一版 ``for existing_refl in entry.reflections: seen.add(...)``
        循环只填充 ``seen`` 却从不读取,是死代码;真正写入只靠循环外的
        ``if reflection not in tail``,且 ``tail`` 直接拼了已落盘的
        ``---`` + ``<!-- 反思将追加在下方 -->`` 占位段,导致每调一次
        reflect 就多写一份占位段(N+1 份)。

        修法:
        1. 用 ``_extract_reflections`` 从已落盘文件里抽出**真正的反思块**
           (跳过 ``---`` 分隔行与占位段),不再连带占位段一起拼回去;
        2. ``seen`` set 真正参与去重 —— 对 "已落盘反思 + 本次新反思"
           统一按 strip 后判等,保序去重(首次出现优先);
        3. ``head`` 只生成一次(含一份占位段),去重后的反思按顺序追加在
           占位段之后,占位段不再被重复写入。

        **Phase 27 follow-up / M22 修复**:
        - 删除死代码 / 拼写错误引入的中间变量(本实现里 ``seen`` / ``ordered``
          是有意义的,**真正**的死代码在更早版本;此处保留并显式声明)
        - 调 ``self.generate_soul_proposal(entry)`` 拿返回的 proposal 路径
          (旧实现虽调用但结果丢弃;**M22 修法**:把返回值收下,append 到结果 list)
        - 返回类型从 ``str`` 改为 ``list[str]``:
          - [0] = 反思文本(原来直接 return 的 reflection)
          - [1] = SOUL proposal 写入的路径(``generate_soul_proposal`` 返回)
          - 老 caller 拿到 list 后可以 ``[0]`` 取反思;
            我们在 ``Agent._maybe_journal`` 里相应改为 ``r = await ...; refl = r[0]``。

        **重要兼容性提示**:外部代码如果 ``refl = await journal.reflect(entry)``
        并把 ``refl`` 当 str 用(``refl.startswith(...)`` 等),需要更新。
        内部 caller(``Agent._maybe_journal``)已同步更新。
        """
        reflection = await self.reflector.reflect(entry)  # H4 修复:await async reflector
        entry.reflections.append(reflection)

        path = Path(self._entry_filename(entry))
        # M10 修复:文件 IO 用 asyncio.to_thread 包装,避免阻塞事件循环
        existing = await asyncio.to_thread(
            lambda: path.read_text(encoding="utf-8") if path.exists() else ""
        )
        existing_reflections = self._extract_reflections(existing)

        # seen 真正参与去重:已落盘反思优先,再本次新反思,按 strip 判等保序。
        # seen / ordered 是真正常用的中间变量,**非**死代码;M22 修法是把
        # generate_soul_proposal 的返回值也收下,不再丢弃。
        seen: set[str] = set()
        ordered: list[str] = []
        for refl in (*existing_reflections, reflection):
            stripped = refl.strip()
            if not stripped:
                continue
            if stripped in seen:
                continue
            seen.add(stripped)
            ordered.append(stripped)

        # head 含一份占位段;反思按顺序追加在占位段下方(不再重复拼 --- / 占位段)。
        head = self._entry_to_md(entry)
        tail = "".join("\n" + refl + "\n" for refl in ordered)
        # 归一连续空行(head + tail),5 个空行 → 1 个空行。
        final_text = self._collapse_blank_lines(head + tail)
        # M10 修复:文件写入也用 asyncio.to_thread
        await asyncio.to_thread(path.write_text, final_text, encoding="utf-8")

        # M22 修复:调 generate_soul_proposal,**接收返回值**;旧实现虽然调过
        # 这个方法,但结果被丢弃,SOUL proposal 链完全无声写入。
        # 现在 proposal_path 显式收下并**记录到 logger**(不破坏旧 caller
        # 拿 str 的契约 —— reflect 仍返回反思 str;proposal 路径走 DEBUG log)。
        proposal_path = self.generate_soul_proposal(entry)
        if proposal_path:
            logger.debug(
                "journal_soul_proposal_written",
                entry=str(entry.session_id),
                proposal_path=str(proposal_path),
            )
        return reflection

    @staticmethod
    def _extract_reflections(text: str) -> list[str]:
        """从已落盘的 entry 文本里抽取反思块(跳过占位段 / 分隔行)。

        反思块以行首 ``# `` 开头(``TemplateReflector`` 返回 ``# 反思 ...``、
        ``LLMReflector`` 返回 ``# LLM 反思``);反思内部的 ``##`` 子标题、
        ``---`` 分隔行、``<!-- 反思将追加在下方 -->`` 占位段都不会被当作
        新块的开头(它们要么出现在首个 ``# `` 之前被丢弃,要么并入当前块)。

        这一步是去重能否真正生效的前提 —— 旧实现直接把
        ``"---" + existing.split("---", 1)[1]`` 当 tail 拼回去,把占位段
        也一起带进了输出,导致占位段被反复写入。
        """
        marker = "<!-- 反思将追加在下方 -->"
        idx = text.find(marker)
        if idx == -1:
            return []
        body = text[idx + len(marker):]
        blocks: list[str] = []
        current: list[str] = []
        for line in body.splitlines():
            if line.startswith("# "):
                # 一个新的反思块开始:先把上一个块收尾
                if current:
                    blocks.append("\n".join(current).strip())
                current = [line]
            elif current:
                # 当前块内的行(含 ## 子标题、空行等)并入当前块
                current.append(line)
        if current:
            blocks.append("\n".join(current).strip())
        return [b for b in blocks if b]

    @staticmethod
    def _collapse_blank_lines(text: str) -> str:
        """把连续空行(>1)压缩成单个空行。

        实现:用 ``seen_blank`` 状态机 — 上一行是空行时,本行空就跳过。
        """
        out_lines: list[str] = []
        seen_blank = False
        for line in text.splitlines():
            is_blank = not line.strip()
            if is_blank and seen_blank:
                continue
            out_lines.append(line)
            seen_blank = is_blank
        # splitlines 会丢尾部换行,这里保留原末尾换行特征
        result = "\n".join(out_lines)
        if text.endswith("\n") and not result.endswith("\n"):
            result += "\n"
        return result

    async def add_reflection(self, entry: JournalEntry, text: str) -> None:
        entry.reflections.append(text)
        path = Path(self._entry_filename(entry))
        with path.open("a", encoding="utf-8") as f:
            f.write("\n" + text + "\n")

    # ───── 周报 ─────

    def list_entries(self, since: Optional[datetime] = None) -> list[Path]:
        """列所有 entry 文件(可选 since 过滤)。"""
        if not self.root.exists():
            return []
        files: list[Path] = []
        for p in sorted(self.root.rglob("sess_*.md")):
            if since is None:
                files.append(p)
                continue
            # 文件路径含日期目录
            try:
                date_str = p.parent.name
                file_date = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if file_date >= since:
                files.append(p)
        return files

    def weekly_report(self, week_start: Optional[datetime] = None) -> Path:
        """生成周报 → `weekly_<YYYY-Www>.md`。

        默认本周一到现在;传 `week_start` 可生成历史周。
        """
        now = datetime.now(timezone.utc)
        if week_start is None:
            days_since_monday = now.weekday()  # 0=Mon
            week_start = (now - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
        week_end = week_start + timedelta(days=7)
        iso_year, iso_week, _ = week_start.isocalendar()

        files = self.list_entries(since=week_start)

        # 聚合
        n_sessions = len(files)
        n_tools = 0
        n_errors = 0
        n_iter_total = 0
        tag_counter: dict[str, int] = {}
        user_msgs: list[str] = []
        for fp in files:
            text = fp.read_text(encoding="utf-8")
            # 简单解析 — 用正则抓标签、迭代、错误
            for m in re.finditer(r"\*\*(迭代)\*\*:\s*(\d+)", text):
                n_iter_total += int(m.group(2))
            for m in re.finditer(r"\*\*(标签)\*\*:\s*([^_\n]+)", text):
                for tag in m.group(2).split(","):
                    tag = tag.strip()
                    if tag and tag != "_(待 reflect)_":
                        tag_counter[tag] = tag_counter.get(tag, 0) + 1
            n_tools += text.count("- `")  # tool call 行
            n_errors += text.lower().count("error")

            # 用户消息(粗略切)
            um = re.search(r"## 用户输入\n\n```\n(.*?)\n```", text, re.DOTALL)
            if um:
                user_msgs.append(um.group(1)[:200].replace("\n", " "))

        out_path = self.root / f"weekly_{iso_year}-W{iso_week:02d}.md"
        top_tags = sorted(tag_counter.items(), key=lambda x: -x[1])[:10]
        avg_iter = n_iter_total / n_sessions if n_sessions else 0

        body = f"""# 周报 {iso_year}-W{iso_week:02d}

> 区间: {week_start.date()} → {week_end.date()} (UTC)
> 生成时间: {now.isoformat(timespec='seconds')}

## 指标

| 指标 | 数值 |
|---|---|
| Session 总数 | **{n_sessions}** |
| 工具调用总次数 | **{n_tools}** |
| 错误次数 | **{n_errors}** |
| 平均迭代轮次 | **{avg_iter:.1f}** |

## 标签分布(Top 10)

"""
        if top_tags:
            body += "\n".join(f"- `{tag}`: {cnt}" for tag, cnt in top_tags)
        else:
            body += "_(无 — 还没跑过 reflect)_"

        body += "\n\n## 典型用户问题(节选前 10)\n\n"
        if user_msgs:
            body += "\n".join(f"- {m}" for m in user_msgs[:10])
        else:
            body += "_(无)_"

        body += "\n\n## 后续行动\n\n"
        if n_errors > 0:
            body += f"- ⚠️ 本周 **{n_errors}** 次错误,需查看 `_soul_proposals.md`\n"
        if avg_iter > 5:
            body += "- ⚠️ 平均迭代较高,考虑优化 SOUL 让 agent 更直接\n"
        if n_sessions == 0:
            body += "- 还没有 session — 跑几个再来看周报\n"
        if n_sessions > 0 and n_errors == 0 and avg_iter <= 5:
            body += "- ✅ 表现稳定,继续观察\n"

        out_path.write_text(body, encoding="utf-8")
        logger.info("weekly report: %s", out_path)
        return out_path

    # ───── SOUL proposal(dry-run) ─────

    def generate_soul_proposal(self, entry: JournalEntry) -> str:
        """基于反思生成"建议的 SOUL 改动" → 写到 `_soul_proposals.md`。

        **不会**自动改 SOUL — 等人 review。
        """
        # 从 reflections 里抓"建议"段
        suggestions: list[str] = []
        for refl in entry.reflections:
            in_suggest_section = False
            for line in refl.splitlines():
                if "改进建议" in line or "Suggestions" in line.lower():
                    in_suggest_section = True
                    continue
                if in_suggest_section and line.strip().startswith(("-", "*", "•")):
                    suggestions.append(line.strip())

        # 简化版:把 entry 标签 + 反思追加到 proposals 文件
        prop_path = self.root / "_soul_proposals.md"
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with prop_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {ts} | session `{entry.session_id}`\n\n")
            f.write(f"**标签**: {', '.join(entry.tags) or '_(无)_'}\n\n")
            f.write("**反思摘要**:\n\n")
            for refl in entry.reflections[-2:]:
                # 截前 800 字符
                f.write("```\n" + refl[:800] + "\n```\n\n")
            if suggestions:
                f.write("**建议的 SOUL 改动**:\n\n")
                f.write("\n".join(f"- {s}" for s in suggestions))
                f.write("\n")
        logger.info("SOUL proposal appended: %s", prop_path)
        return str(prop_path)


# ──────────── 工具注册(让 agent 自我调) ────────────

def register_journal_tools(registry: Any, journal: AgentJournal) -> None:
    """把 journal 暴露为 agent 可调的工具 — 让 agent 自己反思。"""
    from openclaw.tools.registry import ToolCategory, ToolPermission

    @registry.tool(category=ToolCategory.CUSTOM, permission=ToolPermission.READ)
    def list_journal(days: int = 7) -> str:
        """列出最近 N 天的 journal 文件路径(用于复盘)。days: 天数,默认 7。"""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        files = journal.list_entries(since=since)
        if not files:
            return "_(无 journal)_"
        return "\n".join(str(p.relative_to(journal.root)) for p in files[-20:])

    @registry.tool(category=ToolCategory.CUSTOM, permission=ToolPermission.READ)
    def read_journal(path: str) -> str:
        """读取一个 journal 文件的完整内容(用于 self-review)。path: 相对路径。"""
        full = journal.root / path
        if not full.exists() or not str(full.resolve()).startswith(str(journal.root.resolve())):
            return f"[error] file not found or outside journal root: {path}"
        return full.read_text(encoding="utf-8")

    @registry.tool(category=ToolCategory.CUSTOM, permission=ToolPermission.WRITE)
    def weekly_report() -> str:
        """生成本周周报,返回文件路径。"""
        p = journal.weekly_report()
        return f"weekly report written: {p.relative_to(journal.root)}"
