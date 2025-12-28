# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CEO Dashboard is a multi-agent orchestration interface for Claude Code CLI. It spawns and manages multiple Claude CLI processes across different projects, providing an inbox-first UI where agents only surface when they need human input.

## Commands

```bash
# Run the server
uv run uvicorn main:app --reload

# Run core regression tests (fast, verifies essential communication)
uv run python test_core.py

# Run full E2E tests (slower, tests approval flows)
uv run python test_e2e.py

# Run browser tests with Playwright
uv run python test_browser.py
```

## Architecture

```
Browser <--WebSocket--> FastAPI (main.py) <--subprocess stdin/stdout--> Claude CLI
                              |
                              +--> MCP Permission Server (mcp_permission_server.py)
```

**Key files:**
- `main.py` - FastAPI backend, WebSocket handling, Claude CLI process management
- `mcp_permission_server.py` - MCP protocol server for tool approval in normal/auto-edit modes
- `static/app.js` - Frontend state management, WebSocket client, rendering
- `static/index.html` - Single-page UI
- `static/style.css` - Styles

**Communication with Claude CLI:**
- Uses `--output-format stream-json` and `--input-format stream-json`
- Prompts sent via stdin as JSON: `{"type": "user", "message": {"role": "user", "content": [...]}}`
- Output parsed line-by-line as JSON, broadcast to all WebSocket clients

**Permission Modes:**
1. `plan` - Read-only, Claude can only analyze (`--permission-mode plan`)
2. `normal` - All edits/bash require approval via MCP permission server
3. `auto-edit` - File edits auto-approved, bash requires approval
4. `yolo` - Everything auto-approved (`--dangerously-skip-permissions`)

## Critical Code Sections

Look for these markers in `main.py`:
- `=== CRITICAL: PERMISSION FLOW ===` - How approvals work via MCP + WebSocket
- `=== CRITICAL: MCP CONFIG ===` - How MCP server is configured for Claude CLI
- `=== CRITICAL: PATTERN MATCHING ===` - "Yes to all X" auto-approve logic

## Configuration

- `CEO_PROJECTS_DIR` env var - Directory to scan for projects (default: `~/Projects`)
- `CEO_PORT` env var - Server port (default: 8000)
