---
name: emulation-privacy-hygiene
description: Keep private product names out of emulator repos
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [privacy, git, embedded]
    category: embedded-emulation
---

# Privacy hygiene for emulation work

Emulator work touches private product firmware: board names, project names, repository paths,
code. None of it may enter files that can be pushed.

## When to Use

- Before every commit in an emulator, tool or documentation repository.
- When writing board files, tests, docs or chat logs that mention the product.

## Procedure

1. **Rules file outside every repository**, for example `~/.config/embeddyn/chat-redact.tsv`. It
   holds one rule per line:
   - `private<TAB>regex` for names and paths;
   - `replace<TAB>regex<TAB>replacement` for renames.

   Never put the rules file, or its patterns, in a repository.
2. **Neutral names** in repositories: describe the function ("body-contour board",
   `dspic30f5011-bodycontour.repl`), never the product or board number.
3. **Before committing:** run `privacy_scan paths=[<changed dirs>] rules_file=<rules file>`. It
   must return `ok: true`. Fix every match, then scan again.
4. **Chat logs or transcripts** must go through a renderer that applies the rules and drops the
   output of commands that read the private repository.
5. **Commit only when the user asks; push only with the user's explicit OK.**

## Pitfalls

- Search commands you ran yourself (grep patterns) leak name fragments into logs. The redaction
  rules must cover the partial patterns too.
- Generated files (images, traces, disassembly) from product firmware stay in scratch
  directories.

## Verification

- `privacy_scan` returns `ok: true` for every directory about to be committed.
