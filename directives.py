"""AI 输出指令块解析：纯函数，无副作用。"""
import re
from urllib.parse import quote_plus


_COT_OPENERS = (
    "let's", "let me", "i'll ", "i will ", "my response", "my reply",
    "the user", "user's message", "this is a",
    "i should", "i need to", "i want to", "first,", "step 1",
    "okay,", "alright,", "options:", "plan:", "thinking:",
    "draft:", "considering", "analysis:", "breakdown:",
)


def _strip_cot_preamble(raw: str) -> str:
    """
    剥离模型偶尔泄露的思维链。Gemini 这类模型有时会输出几段 markdown
    形式的"分析—方案—草稿"再给真正的回复；我们的真实输出格式是
    "英文一行 + （中文翻译一行）"成对出现，按这个锚点找回真正的回复起点。
    """
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if not raw:
        return raw

    # 1) "Message 1:" / "Final reply:" / "Output:" 等明显起点标记
    marker_re = re.compile(
        r"(?:^|\n)\s*[*_`#>\-]*\s*(?:Message\s*1|Final\s*(?:reply|response|output)|Output|Reply)\s*:?\s*[*_`]*\s*\n",
        re.IGNORECASE,
    )
    m = marker_re.search(raw)
    if m:
        body = raw[m.end():]
        # 后续 "Message 2:" "Message 3:" 转成 [SPLIT]
        body = re.sub(
            r"(?:^|\n)\s*[*_`#>\-]*\s*Message\s*\d+\s*:?\s*[*_`]*\s*\n",
            "\n[SPLIT]\n",
            body,
            flags=re.IGNORECASE,
        )
        return body.strip()

    # 2) 启发式：找到第一组「英文行 + （中文翻译）」配对，前面的一律砍掉
    lines = raw.split("\n")
    has_cn = lambda s: any("一" <= c <= "鿿" for c in s)

    def _looks_like_cn_paren(s: str) -> bool:
        s = s.strip()
        if not s or not has_cn(s):
            return False
        # 第一字符是括号，或整体被括号包住
        return s.startswith(("(", "（")) or (s.endswith((")", "）")) and ("(" in s or "（" in s))

    def _looks_like_real_en(s: str) -> bool:
        s = s.strip()
        if not s or len(s) > 240:
            return False
        # 跳掉明显的 markdown 标记
        if s.startswith(("*", "-", "#", ">", "`", "•", "1.", "2.", "3.")):
            return False
        # 必须 ASCII 字母开头
        if not (s[0].isascii() and s[0].isalpha()):
            return False
        # 排除常见思维链开头
        low = s.lower()
        if any(low.startswith(op) for op in _COT_OPENERS):
            return False
        return True

    for i in range(len(lines) - 1):
        a = lines[i]
        # 找下一非空行
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            break
        b = lines[j]
        if _looks_like_real_en(a) and _looks_like_cn_paren(b):
            return "\n".join(lines[i:]).strip()

    # 3) 兜底：剥掉开头看起来像 CoT 的整段（连续以 *, -, 数字., "Let's", "My response" 开头的行）
    cleaned: list[str] = []
    started = False
    for line in lines:
        if not started:
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if (
                s.startswith(("*", "-", "#", ">", "`", "•"))
                or re.match(r"^\d+\.\s", s)
                or any(low.startswith(op) for op in _COT_OPENERS)
            ):
                continue
            started = True
        cleaned.append(line)
    out = "\n".join(cleaned).strip()
    return out or raw


def parse_bot_directives(raw_bot_reply: str):
    """
    解析 AI 输出中的指令块，返回：
    - clean_reply: 去除所有指令后的纯正文
    - messages_to_send: clean_reply 按 [SPLIT] 拆分后的消息列表
    - reaction_target: "SELF" / "USER"
    - emojis_to_react: 要挂的 emoji 列表
    - action_matches: [ACTION]...[/ACTION] 的 JSON 字符串列表
    """
    # 强力清除思维链与裸 CoT 段落
    raw_bot_reply = _strip_cot_preamble(raw_bot_reply)

    reaction_match = re.search(r'\[REACTION:(.*?)\]', raw_bot_reply, re.DOTALL)
    emojis_to_react = []
    reaction_target = "SELF"
    if reaction_match:
        raw_emojis = reaction_match.group(1).strip()
        if raw_emojis.upper() != "NONE":
            parts_r = [p.strip() for p in raw_emojis.split(',')]
            if parts_r:
                reaction_target = (parts_r[0] or "SELF").upper()
                emojis_to_react = [e for e in parts_r[1:] if e]

    action_matches = re.findall(r'\[ACTION\](.*?)\[/ACTION\]', raw_bot_reply, re.DOTALL)

    # [LINK:kind:query] —— 让 T.S. 推歌/科普时甩一个链接
    link_matches = re.findall(r'\[LINK:([a-zA-Z]+):([^\[\]]+?)\]', raw_bot_reply)

    clean_reply = re.sub(r'\[REACTION:.*?\]\n?', '', raw_bot_reply, flags=re.DOTALL)
    clean_reply = re.sub(r'\[ACTION\].*?\[/ACTION\]\n?', '', clean_reply, flags=re.DOTALL)
    clean_reply = re.sub(r'\[LINK:[a-zA-Z]+:[^\[\]]+?\]\n?', '', clean_reply)
    clean_reply = clean_reply.strip()

    # 先尝试按 [SPLIT] 拆分
    raw_msgs = [msg.strip() for msg in clean_reply.split('[SPLIT]') if msg.strip()]

    # 对每一条消息做"一句话一段"的补拆（模型有时忘记加 [SPLIT]）
    expanded: list[str] = []
    for msg in raw_msgs:
        if '\n' in msg:
            expanded.extend(_auto_split_bilingual(msg))
        else:
            expanded.append(msg)
    raw_msgs = [m for m in (x.strip() for x in expanded) if m]

    messages_to_send = [re.sub(r'\n{2,}', '\n', msg.strip()) for msg in raw_msgs if msg.strip()]

    # 把 LINK 解析成 URL，作为额外消息追加在末尾，每条 URL 单独发
    seen_urls: set[str] = set()
    for kind, query in link_matches:
        url = _resolve_link_directive(kind, query)
        if url and url not in seen_urls:
            seen_urls.add(url)
            messages_to_send.append(url)

    return clean_reply, messages_to_send, reaction_target, emojis_to_react, action_matches


def _resolve_link_directive(kind: str, query: str) -> str | None:
    """把 [LINK:type:query] 解析成对应平台的搜索 URL；零外部依赖。"""
    q = (query or "").strip()
    if not q:
        return None
    qenc = quote_plus(q)
    k = (kind or "").strip().lower()
    if k in ("music", "song", "spotify"):
        return f"https://open.spotify.com/search/{qenc}"
    if k in ("youtube", "yt", "mv"):
        return f"https://www.youtube.com/results?search_query={qenc}"
    if k in ("book", "books"):
        return f"https://www.google.com/search?tbm=bks&q={qenc}"
    if k in ("wiki", "wikipedia"):
        return f"https://zh.wikipedia.org/wiki/Special:Search?search={qenc}"
    if k in ("web", "search", "google"):
        return f"https://www.google.com/search?q={qenc}"
    if k in ("map", "maps"):
        return f"https://www.google.com/maps/search/{qenc}"
    return None


def _auto_split_bilingual(text: str) -> list[str]:
    """
    当模型忘记 [SPLIT] 时，检测双语消息边界并自动拆分。
    边界判定：中文翻译行（含中文字符）后，下一非空行是新的英文句子（ASCII字母开头）。
    """
    lines = text.split('\n')
    chunks: list[list[str]] = []
    current: list[str] = []

    def _has_chinese(s: str) -> bool:
        return any('\u4e00' <= c <= '\u9fff' for c in s)

    def _looks_like_cn_close(s: str) -> bool:
        if not s or not _has_chinese(s):
            return False
        # 常见句末：括号、中英标点、引号
        return s.endswith((')', '）', '。', '！', '？', '；', '…', '」', '"', '”', '"'))

    def _looks_like_en_start(s: str) -> bool:
        if not s:
            return False
        # 跳过常见前置markdown/引用符号
        stripped = s.lstrip(' \t>*_~`"\'-')
        if not stripped:
            return False
        ch = stripped[0]
        return ch.isascii() and ch.isalpha()

    for i, line in enumerate(lines):
        current.append(line)
        stripped = line.strip()
        if _looks_like_cn_close(stripped):
            # 找到下一个非空行
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and _looks_like_en_start(lines[j].strip()):
                chunks.append(current)
                current = []
    if current:
        chunks.append(current)
    if len(chunks) > 1:
        return ['\n'.join(c).strip() for c in chunks if '\n'.join(c).strip()]
    return [text]
