---
name: Diagnose before brute-forcing
description: User corrected me for repeatedly trying to force piped input instead of reading the code and fixing the actual problem. Always understand root cause first.
type: feedback
---

Diagnose the root cause before attempting workarounds.

**Why:** I tried piping "yes" and empty strings into interactive prompts multiple times instead of reading the code to understand the env var conflict. Prashil called this out explicitly.

**How to apply:** When something fails, read the relevant code and understand why before retrying. Never brute-force past interactive prompts — fix the code to support non-interactive mode properly.
