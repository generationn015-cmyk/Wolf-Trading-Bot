# SKILL_VETTING.md

## Purpose
Evaluate external tools, skills, and integrations before adopting them into Wolf.

## Rule
Nothing gets installed blindly. Every incoming tool/skill/integration must be reviewed.

## Vetting Checklist

### 1. Capability Check
- What does this actually add?
- Do we already cover this with existing tools?
- Does it meaningfully improve the system?

### 2. Overlap Check
- Does it duplicate existing tools or workflow rules?
- Does it create another layer for something we already handle?

### 3. Security Check
Reject or escalate if it:
- Requests credentials without a clear reason
- Touches sensitive files without justification
- Sends data externally without a necessary purpose
- Runs opaque commands or downloads unknown code
- Introduces wallet setup, live order execution, or autonomous financial actions without explicit Jefe authorization

### 4. Foundation Fit
- Does it simplify the system?
- Does it strengthen a weak area?
- Does it fit cleanly with the current build?
- Or does it add clutter and another thing to maintain?

## Output Format
For every tool/skill reviewed:
- **Summary** — what it is
- **What's useful** — best parts worth keeping
- **What overlaps** — where it duplicates what we have
- **Recommendation** — install / adapt / reject
- **Integration plan** — what actually gets added, if anything

## Standard
Adapt the useful parts. Protect the foundation. Never install execution-adjacent tools without explicit authorization.
