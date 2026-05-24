---
name: Always show progress and time estimates
description: User expects clear progress indicators (X/Y done) and time estimates for any long-running operation. Silent waits are unacceptable.
type: feedback
---

Always show progress and time estimates for long-running operations.

**Why:** Prashil had no idea if ingest would take 1 minute or 2 hours. A silent process with no feedback is a "very poor way of doing it."

**How to apply:** Any batch operation (ingest, rescan, metadata fetch) must show "N of M done" with elapsed time. CLI and web UI both.
