# Future Work

Ideas and features to consider for CEO Dashboard.

## High Priority

### Structured Job Templates
Replace free-form text input with structured job form:
- Objective
- Constraints (what NOT to do)
- Milestones (checkpoints)
- Deliverables

Generates well-structured prompt for Claude Code. "Quick job" option for simple tasks.

### Plan Mode as Default
Start agents in `--permission-mode plan` by default. Agent produces plan first, CEO reviews, then switches to execution mode.

### Usage/Context Dashboard
- Show aggregate usage across all agents
- Context saturation warning at 80% - prompt for `/compact`
- Cost tracking per agent and total

### Git Worktree Support
- Spawn agents on separate worktrees for parallel work
- Visual indication of which branch/worktree each agent is on
- Prevent agents from stepping on each other

## Medium Priority

### Milestone/Checkpoint Tracking
Show progress as milestones instead of just chat. Agent pauses at gates for CEO approval.

Concern: May fight Claude Code's autonomous nature. Need to balance.

### Background Task Panel
- Show running background tasks (tests, builds, linters)
- Results feed back into dashboard
- Correlate with `/bashes` command

### Command Library
Surface project's `.claude/commands/` directory:
- List available custom commands
- One-click to apply to current agent
- Preview command content

### Session State Files
Auto-manage DECISIONS.md / STATE.md files:
- Track key decisions made during session
- Help new sessions rehydrate context quickly
- Reduce need for re-explanation

## Lower Priority / Research

### Subagent Specialization
If/when Claude Code supports true parallel subagents:
- Explorer: map codebase, find files
- Implementer: edits code, runs tests
- Test Engineer: coverage, edge cases
- Reviewer: code review, security

Current limitation: Can only spawn orchestrator, not true parallel agents.

### Extended Thinking Controls
- Toggle `ultrathink` mode per agent
- Thinking budget configuration
- Use sparingly for architectural/ambiguous tasks

### Sandboxing
Leverage Claude Code's `/sandbox` for safer autonomy:
- Isolate filesystem/network
- Reduce permission friction
- Enable more autonomous operation

## References

Based on Claude Code best practices research (June-Dec 2025):
- Plan Mode for multi-file changes
- Custom slash commands for reusable workflows
- Git worktrees for parallel sessions
- Structured task design beats clever prompting
- 10-20 min autonomy windows work best, then checkpoint
