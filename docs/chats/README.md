# Development chats

Transcripts of the Claude Code sessions that built embeddyn's mechatronics co-simulation,
the SPECT body-contour emulation and the dsPIC30F5011 Renode core, rendered to Markdown.

| File | Period | Content |
|---|---|---|
| [2026-11-21-to-25-part1-main.md](2026-11-21-to-25-part1-main.md) | 2026-11-21 – 2026-11-25 | Project review and roadmap; Renode/ngspice/Webots/MuJoCo/FMI co-simulation; fault injection; bus-frame faults; power electronics; light-curtain optics and the STM32 prototype; the dsPIC30F5011 decision (master/slave, iC-NT/iC-ME, CANopen); XC16 install; workflow guide; the dsPIC30 CPU core and its sim30 differential tests |
| [2026-11-25-part2-dspic-core.md](2026-11-25-part2-dspic-core.md) | 2026-11-25 | DSP instructions, modulo/bit-reversed addressing (checked against sim30 and the MPLAB X simulator model), the core inside Renode's tlib, peripherals |
| [2026-11-24-branch-messages.md](2026-11-24-branch-messages.md) | 2026-11-24 | Messages present only in an earlier copy of the transcript (a side branch) |

Each file keeps every user message, the assistant's replies and every tool call with its
result, in order. Left out: hidden reasoning, harness bookkeeping and system reminders.
E-mail addresses are redacted, and tool inputs over 8,000 and outputs over 4,000 characters
are shortened (marked inline). Context compactions appear as "Summary of earlier conversation".

Regenerate with [render_chat.py](render_chat.py):

```bash
python3 render_chat.py ~/.claude/projects/<project>/<session>.jsonl out.md --title "..." \
    --redact-file ~/.config/embeddyn/chat-redact.tsv
```

The redaction rules (private product names and paths) live in that file outside the
repository; output of tool calls that read private locations is omitted.
