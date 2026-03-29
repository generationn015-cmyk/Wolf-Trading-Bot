# MEMORY_SYSTEM.md

## Purpose
Keep memory clean, durable, and useful across sessions.

## Memory Hierarchy

### 1. SESSION-STATE.md
Active working memory — current focus, blockers, pending actions, in-flight decisions.
Not a long-term archive. Clear stale items regularly.

### 2. memory/YYYY-MM-DD.md
Raw daily notes — what happened, rough progress, session snapshots.

### 3. MEMORY.md
Curated long-term memory — stable preferences, lasting decisions, recurring truths.
Keep lean. Promote only what's worth keeping forever.

### 4. .learnings/
Repeated mistakes, system fixes, lessons worth keeping.
Create .learnings/ directory when first lesson worth capturing arrives.

### 5. memory/projects/*.md
Per-project stable context — objectives, decisions, blockers, next steps.

## Memory Routing Rule
Before writing memory, decide what kind it is:
- Session-only → SESSION-STATE.md
- Daily raw note → memory/YYYY-MM-DD.md
- Long-term durable context → MEMORY.md
- Repeated lesson or failure → .learnings/
- Project-specific stable context → memory/projects/*.md

## Smart Loading (heartbeat)
1. Read MEMORY.md (~curated long-term)
2. Read memory/projects/*.md for active projects
3. Only load daily notes when asked about specific past work
4. Only run search on-demand

This keeps token cost low on every turn.

## Hygiene Rule
At natural checkpoints:
- Clear stale SESSION-STATE items
- Promote important daily notes into MEMORY.md when warranted
- Move repeated patterns into .learnings/
- Keep project files current if work is active

## Phase Reset Rule
When a thread becomes mostly historical baggage instead of useful working context:
- Summarize important state into files
- Prepare a clean handoff point
- Recommend a reset or new phase
- Do not reset silently in the middle of active work
