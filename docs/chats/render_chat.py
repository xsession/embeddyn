#!/usr/bin/env python3
"""Render a Claude Code session transcript (JSONL) as Markdown for docs/chats.

Keeps every user message, assistant text and tool call with its result, in order.
Drops hidden reasoning, harness bookkeeping records and <system-reminder> blocks;
redacts e-mail addresses; caps very long tool inputs/outputs (noted inline).

    render_chat.py transcript.jsonl out.md --title T [--only-not-in other.jsonl] [--redact-file F]

--redact-file: rules kept OUTSIDE the repository (so private names never reach git), one per
line, tab-separated:
    replace<TAB>regex<TAB>replacement   every match in any text is replaced
    private<TAB>regex                   a tool call whose input matches has its output omitted,
                                        and output lines that match are dropped
Lines starting with # are comments.
"""
import argparse, json, re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
KEEP_EMAILS = {"noreply@anthropic.com"}


# Markdown links whose target is not a web URL point at files of the original working tree, which
# do not exist relative to docs/chats: render them as text plus the target in code.
LOCAL_LINK = re.compile(r"\[([^\]\n]+)\]\((?!https?://|mailto:|#)([^)\s]+)\)")

REPLACE: list = []
PRIVATE: list = []


def load_rules(path):
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if parts[0] == "replace" and len(parts) == 3:
            REPLACE.append((re.compile(parts[1], re.I), parts[2]))
        elif parts[0] == "private" and len(parts) == 2:
            PRIVATE.append(re.compile(parts[1], re.I))


def is_private(text):
    return any(p.search(text) for p in PRIVATE)


def clean(text, output=False):
    text = REMINDER.sub("", text).replace("\r\n", "\n").replace("\r", "")
    if output and PRIVATE:
        text = "\n".join(l if not is_private(l) else "[line from a private location omitted]" for l in text.split("\n"))
    for rx, rep in REPLACE:
        text = rx.sub(rep, text)
    text = LOCAL_LINK.sub(lambda m: f"{m.group(1)} (`{m.group(2)}`)", text)
    return EMAIL.sub(lambda m: m.group(0) if m.group(0) in KEEP_EMAILS else "[email redacted]", text).strip()


def cap(text, n):
    return text if len(text) <= n else text[:n] + f"\n… [{len(text) - n} more characters not shown]"


def fence(text):
    ticks = "````" if "```" in text else "```"
    return f"{ticks}\n{text}\n{ticks}"


def result_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if c.get("type") == "text":
            parts.append(c["text"])
        elif c.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def records(path):
    for line in open(path, encoding="utf-8"):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") in ("user", "assistant"):
            yield o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl"); ap.add_argument("out"); ap.add_argument("--title", required=True)
    ap.add_argument("--only-not-in"); ap.add_argument("--in-max", type=int, default=8000)
    ap.add_argument("--out-max", type=int, default=4000)
    ap.add_argument("--redact-file")
    a = ap.parse_args()
    if a.redact_file:
        load_rules(a.redact_file)
    private_calls = set()
    skip = set()
    if a.only_not_in:
        skip = {o.get("uuid") for o in records(a.only_not_in)}
    out = [f"# {a.title}", "", f"Source transcript: `{a.jsonl.rsplit('/', 1)[-1]}`. Hidden reasoning, harness "
           "records and system reminders are omitted; e-mail addresses are redacted; tool inputs over "
           f"{a.in_max} and tool outputs over {a.out_max} characters are shortened.", ""]
    for o in records(a.jsonl):
        if o.get("uuid") in skip:
            continue
        ts = (o.get("timestamp") or "")[:19].replace("T", " ")
        msg = o.get("message", {})
        content = msg.get("content")
        if o["type"] == "user":
            if isinstance(content, str):
                t = clean(content)
                if t:
                    label = "Summary of earlier conversation (context compaction)" if o.get("isCompactSummary") or t.startswith("This session is being continued") else "User"
                    out += [f"## {label} — {ts}", "", t, ""]
                continue
            for c in content or []:
                if c.get("type") == "text":
                    t = clean(c["text"])
                    if t and not o.get("isMeta"):
                        out += [f"## User — {ts}", "", t, ""]
                elif c.get("type") == "tool_result":
                    if c.get("tool_use_id") in private_calls:
                        out += ["*(output from a private location omitted)*", ""]
                        continue
                    t = clean(result_text(c.get("content")), output=True)
                    if t:
                        out += ["<details><summary>Result</summary>", "", fence(cap(t, a.out_max)), "", "</details>", ""]
        else:
            for c in content or []:
                if c.get("type") == "text":
                    t = clean(c["text"])
                    if t:
                        out += [f"### Assistant — {ts}", "", t, ""]
                elif c.get("type") == "tool_use":
                    inp = c.get("input", {})
                    raw = json.dumps(inp, ensure_ascii=False, indent=1)
                    if is_private(raw):
                        private_calls.add(c.get("id"))
                    desc = inp.get("description") if isinstance(inp, dict) else None
                    desc = clean(desc) if desc else None
                    body = clean(raw)
                    out += [f"**Tool: {c.get('name')}**" + (f" — {desc}" if desc else ""), "",
                            fence(cap(body, a.in_max)), ""]
    open(a.out, "w", encoding="utf-8").write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
