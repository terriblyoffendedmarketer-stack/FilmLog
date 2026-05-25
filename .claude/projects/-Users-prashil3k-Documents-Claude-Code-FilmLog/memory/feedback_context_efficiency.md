---
name: Be context-efficient — avoid Chrome automation bloat
description: Prashil has burned through 3-4 context windows already. Minimize Chrome tool usage, maximize code output per session.
type: feedback
---

Avoid Chrome automation tools unless truly needed (e.g. final visual QA). They eat context window fast. Do as much building as possible through code, only use browser at the end or for critical checks.

**Why:** This is the 3rd or 4th context window opened for this project. Each restart loses quality and continuity.

**How to apply:** Write code, test via CLI/curl where possible, save browser interaction for end-of-session verification.
