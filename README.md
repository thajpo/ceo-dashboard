# CEO Dashboard

A multi-agent orchestration dashboard for Claude Code CLI. Manage multiple Claude agents across different projects from a single inbox-first interface.

Think of it as "CEO mode" - you delegate work to agents, they report back only when they need your input. Maximize throughput, minimize interruption.

![CEO Dashboard](https://github.com/user-attachments/assets/placeholder.png)

## Features

- **Inbox-first design** - Only see what needs your attention
- **Multi-project support** - Run agents on multiple projects simultaneously  
- **Auto-return flow** - Answer a question → automatically back to inbox
- **Agent modes** - Normal (approval required), Plan (read-only), YOLO (full autonomy)
- **Real-time streaming** - WebSocket-based live updates from all agents
- **Session persistence** - Agents maintain conversation context

## Requirements

- Python 3.13+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and configured
- [uv](https://github.com/astral-sh/uv) package manager

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/ceo-dashboard.git
cd ceo-dashboard
uv sync
```

## Usage

```bash
uv run uvicorn main:app --reload
```

Open http://127.0.0.1:8000

### Quick Start

1. Click **+ Add Project** to select a project from `~/Projects/`
2. Choose an agent mode:
   - **Normal** - Agent asks for approval before making changes
   - **Plan** - Read-only analysis mode
   - **YOLO** - Full autonomy, no approval needed
3. The agent reports initial project status to your inbox
4. Click inbox items to open conversation view
5. Respond and you're automatically returned to inbox
6. Agents work in background, only surfacing when they need you

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  CEO Dashboard                        [+ Add Project]   │
├─────────────────────────────────────────────────────────┤
│                                                         │
│   ┌─────────────────────────────────────────────────┐   │
│   │ project-alpha · 2m ago                          │   │
│   │ "Should I use Redis or PostgreSQL?"             │   │
│   │                              [Needs your input] │   │
│   └─────────────────────────────────────────────────┘   │
│                                                         │
│   ─────────── Working ───────────                       │
│   │ api-service    Refactoring auth module...       │   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

- **Backend**: FastAPI + WebSocket for real-time communication
- **Frontend**: Vanilla JS, no build step
- **Agent Runtime**: Spawns Claude Code CLI processes with `--output-format stream-json`

## How It Works

1. Each project gets an orchestrator agent (Claude Code CLI process)
2. Agents stream output via WebSocket to all connected clients
3. Backend classifies messages: questions, approvals, completions
4. Only "interrupts" surface to the inbox
5. Working agents show in a collapsed footer section
6. Session IDs are preserved for conversation continuity

## Configuration

Projects are loaded from `~/Projects/` by default. To change this, edit `PROJECTS_DIR` in `main.py`:

```python
PROJECTS_DIR = Path.home() / "Projects"  # Change this
```

## Development

Run browser tests:
```bash
uv run python test_browser.py
```

This uses Playwright to capture screenshots and verify UI behavior.

## License

MIT
