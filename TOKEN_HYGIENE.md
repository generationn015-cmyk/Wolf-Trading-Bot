# TOKEN_HYGIENE.md

## Core Principle
Reduce waste before reducing model quality.

## Rules

### 1. Keep always-loaded docs lean
Files that load every session should stay compact and intentional.

### 2. Use model calls only where judgment is needed
Rule-based logic, file reads, and structured data ops cost nothing.
Reserve model calls for reasoning, synthesis, and decisions.

### 3. Don't research the same thing twice
If context was built, store it. Don't rebuild from scratch next session.
Files are cheaper than repeated reasoning.

### 4. Use Gemini as challenger, not parallel
Don't run Claude + Gemini on every single task.
Use Gemini selectively when a second opinion genuinely matters.

### 5. Perplexity on-demand, not reflexively
Only call Perplexity when external market/news context is actually needed.
Not on every message.

### 6. Audit file growth
If hot-path files get bloated, move rarely-needed material to reference files.

### 7. Automate repetitive background work
Recurring low-value checks should be scripted or cron-based, not premium agent turns.

### 8. Phase Reset Rule
When a thread becomes mostly historical baggage:
- Summarize into files
- Prepare a clean handoff
- Recommend a new phase
- Never reset silently during active work
