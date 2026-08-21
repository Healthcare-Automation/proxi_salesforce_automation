---
name: kimedics-progress-tracker
description: Use after ANY meaningful change to Kimedics → Salesforce automation — shipped code, a deploy, a fix, a config or data change, or a blocker hit or cleared. Client-facing project, so the Notion Tickets board must reflect it. Not for reads, searches, planning, or routine intermediate steps.
---

# Kimedics → Salesforce automation — keep the client board current

This is a **client-facing** project. Work that is not on the board is work the client cannot see.

Everything goes on the **Tickets** board (`38e23b11-7dfb-80a6-b9a4-d0c829d3a981`) via the global
helper. The Weekly To Dos / Daily Tasks boards are retired as of 2026-08-11 — don't write to them.

This project's engagement alias is **`kimedics`**, which fills both `Project Link` and `Client`.

```bash
# 1. Reuse before creating — one ticket per project per session
python3 ~/.claude/tools/notion_ticket.py list --engagement kimedics

# 2. Ship something → file it, every field filled
python3 ~/.claude/tools/notion_ticket.py create \
  --title "<plain-English headline>" \
  --engagement kimedics \
  --status Done --priority Medium --category Reliability \
  --problem  "<one line: what the client experienced>" \
  --solution "<the permanent fix, plain English>"

# 3. More progress on the same thing → extend it
python3 ~/.claude/tools/notion_ticket.py update --match "<words from the title>" \
  --status Done --note "<one line>"
```

See the global **`progress-tracker`** skill for how to choose Priority, Category, Backend Status
and Acknowledged, and for the full field reference. Rules that matter most here:

- **Plain English only.** No file names, function names, IDs or commit hashes — a client reads these.
- **Paste the ticket URL** when you report the work back. The helper prints it.
- **Reads, searches, planning and intermediate steps are not logged.**
