"""
CEO Dashboard - Multi-Agent Claude Code Manager

This FastAPI application manages multiple Claude Code CLI instances,
providing a web UI for monitoring and interacting with agents working
on different projects.

=== ARCHITECTURE OVERVIEW ===

Browser <--WebSocket--> FastAPI <--subprocess--> Claude CLI
                            |
                            +--> MCP Permission Server (for normal/auto-edit modes)

=== PERMISSION MODES ===

1. Plan Mode:     Claude can only read/analyze, no file changes
2. Normal Mode:   User must approve all edits AND bash commands
3. Auto-Edit:     Edits auto-approved, user approves bash commands
4. YOLO Mode:     Everything auto-approved (requires user to type "agree")

=== CRITICAL LOGIC SECTIONS ===

Look for these markers in the code:
- "=== CRITICAL: PERMISSION FLOW ===" - How approvals work
- "=== CRITICAL: MCP CONFIG ===" - MCP server configuration
- "=== CRITICAL: PATTERN MATCHING ===" - Auto-approve patterns

These sections should not be modified without understanding the full flow.

=== END ARCHITECTURE OVERVIEW ===
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# =============================================================================
# CONFIGURATION
# =============================================================================

# Default to ~/Projects, override with CEO_PROJECTS_DIR env var
PROJECTS_DIR = Path(os.environ.get("CEO_PROJECTS_DIR", Path.home() / "Projects"))

# Internal callback port for MCP server (same as FastAPI)
CALLBACK_PORT = int(os.environ.get("CEO_PORT", 8000))

# Path to MCP permission server script
MCP_SERVER_SCRIPT = Path(__file__).parent / "mcp_permission_server.py"


# =============================================================================
# STATE MANAGEMENT
# =============================================================================

# Agent state: agent_id -> {project, session_id, status, process, mode, mcp_process}
agents: dict[str, dict[str, Any]] = {}

# Connected WebSocket clients
clients: list[WebSocket] = []

# === CRITICAL: PERMISSION FLOW ===
# Pending approval requests: request_id -> asyncio.Future
# When MCP server POSTs to /internal/approve-request, we create a Future here
# and wait for the browser to send an approval_response via WebSocket.
# The Future is resolved when the user clicks Yes/No in the browser.
pending_approvals: dict[str, asyncio.Future] = {}

# Auto-approve patterns: agent_id -> set of patterns
# When user clicks "Yes to all X", we add the pattern here.
# Future requests matching the pattern are auto-approved without prompting.
# Patterns: "Edit", "Write", "Bash:npm", "Bash:git", "Bash:python", etc.
auto_approve_patterns: dict[str, set[str]] = {}
# === END CRITICAL: PERMISSION FLOW ===


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


async def broadcast(message: dict):
    """Send message to all connected WebSocket clients."""
    dead = []
    for client in clients:
        try:
            await client.send_json(message)
        except Exception:
            dead.append(client)
    for client in dead:
        clients.remove(client)


def extract_command_prefix(tool_name: str, tool_input: dict) -> str:
    """
    Extract a pattern prefix from a tool invocation for "Yes to all X" feature.

    === CRITICAL: PATTERN MATCHING ===

    For Bash commands, we extract the first word of the command:
        "npm install express" -> "Bash:npm"
        "git status" -> "Bash:git"
        "python script.py" -> "Bash:python"
        "./run.sh" -> "Bash:./"

    For other tools, we just use the tool name:
        "Edit" -> "Edit"
        "Write" -> "Write"

    These patterns are stored in auto_approve_patterns and checked
    before prompting the user for approval.

    === END CRITICAL: PATTERN MATCHING ===
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        # Extract first word of command
        first_word = command.split()[0] if command.split() else ""
        # Handle paths like ./script.sh or /usr/bin/python
        if first_word.startswith("./"):
            return "Bash:./"
        if first_word.startswith("/"):
            # Extract actual command name from path like /usr/bin/python -> python
            cmd_name = first_word.split("/")[-1]
            return f"Bash:{cmd_name}" if cmd_name else "Bash:/"
        return f"Bash:{first_word}"
    else:
        return tool_name


def should_auto_approve(agent_id: str, tool_name: str, tool_input: dict) -> bool:
    """
    Check if a tool invocation should be auto-approved based on stored patterns.

    Returns True if:
    - The agent has a matching pattern in auto_approve_patterns
    """
    patterns = auto_approve_patterns.get(agent_id, set())
    if not patterns:
        return False

    # Check exact tool name match (e.g., "Edit", "Write")
    if tool_name in patterns:
        return True

    # Check command prefix match for Bash
    prefix = extract_command_prefix(tool_name, tool_input)
    if prefix in patterns:
        return True

    return False


def create_mcp_config(agent_id: str) -> str:
    """
    Create a temporary MCP config file for an agent.

    === CRITICAL: MCP CONFIG ===

    This config tells Claude CLI how to connect to our permission server.
    The server runs as a subprocess with stdin/stdout communication.

    Key fields:
    - command: "python" - interpreter to run the server
    - args: path to script + agent_id + callback_url + cwd
    - The callback_url is where the MCP server POSTs approval requests

    Claude CLI will spawn this as a child process and communicate via stdio.

    === END CRITICAL: MCP CONFIG ===
    """
    agent = agents.get(agent_id, {})
    cwd = str(agent.get("project_path", ""))

    config = {
        "mcpServers": {
            "ceo-perms": {
                "command": sys.executable,  # Use same Python as this process
                "args": [
                    str(MCP_SERVER_SCRIPT),
                    agent_id,
                    f"http://127.0.0.1:{CALLBACK_PORT}/internal/approve-request",
                    cwd,
                ],
            }
        }
    }

    # Write to temp file
    fd, path = tempfile.mkstemp(suffix=".json", prefix=f"mcp-{agent_id}-")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)

    return path


# =============================================================================
# CLAUDE CLI RUNNER
# =============================================================================


async def run_claude(
    agent_id: str,
    prompt: str,
    project_path: Path,
    session_id: str | None = None,
    mode: str = "normal",
):
    """
    Run claude CLI and stream output.

    === CRITICAL: MODE HANDLING ===

    Mode determines CLI flags and permission behavior:

    1. plan:      --permission-mode plan
                  Claude can only read, all modifications blocked

    2. normal:    --permission-prompt-tool mcp__ceo-perms__approve
                  Both edits AND bash commands require user approval

    3. auto-edit: --permission-prompt-tool mcp__ceo-perms__approve
                  --permission-mode acceptEdits
                  Edits auto-approved, bash commands require approval

    4. yolo:      --dangerously-skip-permissions
                  Everything auto-approved, no prompts

    === END CRITICAL: MODE HANDLING ===
    """
    agent = agents.get(agent_id)
    if not agent:
        return

    agent["status"] = "working"
    agent["waiting_on_user"] = False  # Agent is now working
    await broadcast({"agent_id": agent_id, "type": "status", "status": "working"})

    # Build base command - use stream-json input to avoid stdin blocking issues
    cmd = ["claude", "--output-format", "stream-json", "--input-format", "stream-json", "--verbose"]

    mcp_config_path = None

    # === CRITICAL: MODE-SPECIFIC FLAGS ===
    if mode == "plan":
        cmd.extend(["--permission-mode", "plan"])

    elif mode == "normal":
        # Create MCP config for permission handling
        mcp_config_path = create_mcp_config(agent_id)
        cmd.extend(
            [
                "--mcp-config",
                mcp_config_path,
                "--permission-prompt-tool",
                "mcp__ceo-perms__approve",
            ]
        )

    elif mode == "auto-edit":
        # Create MCP config for permission handling
        mcp_config_path = create_mcp_config(agent_id)
        cmd.extend(
            [
                "--mcp-config",
                mcp_config_path,
                "--permission-prompt-tool",
                "mcp__ceo-perms__approve",
                "--permission-mode",
                "acceptEdits",
            ]
        )

    elif mode == "yolo":
        cmd.append("--dangerously-skip-permissions")
    # === END CRITICAL: MODE-SPECIFIC FLAGS ===

    if session_id:
        cmd.extend(["--resume", session_id])

    # Log command to terminal
    print(f"\n[Agent {agent_id}] Starting Claude CLI", file=sys.stderr)
    print(f"[Agent {agent_id}] Mode: {mode}", file=sys.stderr)
    print(f"[Agent {agent_id}] CWD: {project_path}", file=sys.stderr)
    print(f"[Agent {agent_id}] Command: {' '.join(cmd)}", file=sys.stderr)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_path,
        )
        agent["process"] = process

        # Send prompt via stdin in stream-json format
        if process.stdin:
            prompt_msg = json.dumps({
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}]
                }
            })
            # Store user message for persistence
            agent["messages"].append({"role": "user", "content": prompt})
            print(f"[Agent {agent_id}] Sending prompt via stdin", file=sys.stderr)
            process.stdin.write((prompt_msg + "\n").encode())
            await process.stdin.drain()

        # Start stderr reader task
        async def read_stderr():
            if process.stderr:
                async for line in process.stderr:
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if line_str:
                        print(f"[Agent {agent_id}] stderr: {line_str}", file=sys.stderr)

        stderr_task = asyncio.create_task(read_stderr())

        if process.stdout is None:
            return

        # Process stdout (Claude's JSON stream)
        async for line in process.stdout:
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            # Log to terminal
            print(f"[Agent {agent_id}] {line_str[:200]}", file=sys.stderr)

            try:
                data = json.loads(line_str)
                msg_type = data.get("type", "")

                # Broadcast usage if present
                if "usage" in data:
                    await broadcast(
                        {"agent_id": agent_id, "type": "usage", "usage": data["usage"]}
                    )

                # Capture session_id from result message
                if msg_type == "result" and "session_id" in data:
                    agent["session_id"] = data["session_id"]

                # Detect interrupt conditions
                is_interrupt = False
                interrupt_type = None

                if msg_type == "assistant":
                    message = data.get("message", {})
                    content_blocks = message.get("content", [])

                    for block in content_blocks:
                        block_type = block.get("type")

                        if block_type == "text":
                            text = block.get("text", "")
                            # Store assistant text for persistence
                            if text:
                                agent["messages"].append({"role": "assistant", "content": text})
                            # Check for questions
                            if "?" in text and any(
                                q in text.lower()
                                for q in [
                                    "would you",
                                    "should i",
                                    "do you want",
                                    "which",
                                    "what",
                                    "how",
                                    "prefer",
                                    "like me to",
                                ]
                            ):
                                is_interrupt = True
                                interrupt_type = "question"

                        elif block_type == "tool_use":
                            tool_name = block.get("name", "")
                            tool_input = block.get("input", {})

                            # Broadcast tool usage for UI
                            await broadcast(
                                {
                                    "agent_id": agent_id,
                                    "type": "tool",
                                    "tool": {
                                        "name": tool_name,
                                        "input": tool_input,
                                    },
                                }
                            )

                            # Check for special tools
                            if tool_name in ("AskUserQuestion", "AskHuman"):
                                is_interrupt = True
                                interrupt_type = "question"
                            elif tool_name == "ExitPlanMode":
                                is_interrupt = True
                                interrupt_type = "plan"

                if is_interrupt:
                    agent["status"] = "needs_attention"
                    agent["waiting_on_user"] = True  # Needs user input
                    await broadcast(
                        {
                            "agent_id": agent_id,
                            "type": "status",
                            "status": "needs_attention",
                        }
                    )

                # Broadcast the output
                await broadcast(
                    {
                        "agent_id": agent_id,
                        "type": "interrupt" if is_interrupt else "output",
                        "interrupt_type": interrupt_type,
                        "content": data,
                        "raw": line_str,
                    }
                )

            except json.JSONDecodeError:
                # Non-JSON output, send as raw
                await broadcast(
                    {
                        "agent_id": agent_id,
                        "type": "output",
                        "content": {"raw": line_str},
                        "raw": line_str,
                    }
                )

        # Wait for process and stderr task to complete
        await process.wait()
        stderr_task.cancel()

    finally:
        # Cleanup
        agent["process"] = None
        agent["status"] = "idle"
        agent["waiting_on_user"] = True  # Agent finished turn, waiting for user

        # Remove temp MCP config file
        if mcp_config_path and os.path.exists(mcp_config_path):
            os.unlink(mcp_config_path)

        await broadcast({"agent_id": agent_id, "type": "status", "status": "idle"})
        print(f"[Agent {agent_id}] Claude CLI finished", file=sys.stderr)


# =============================================================================
# API ENDPOINTS
# =============================================================================


@app.get("/projects")
async def list_projects():
    """List all directories in ~/Projects/."""
    if not PROJECTS_DIR.exists():
        return {"projects": []}

    projects = [
        d.name
        for d in PROJECTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ]
    return {"projects": sorted(projects)}


@app.post("/agents")
async def create_agent(data: dict):
    """Create a new agent for a project."""
    project = data.get("project")
    if not project:
        raise HTTPException(status_code=400, detail="project required")

    project_path = PROJECTS_DIR / project
    if not project_path.exists():
        raise HTTPException(status_code=404, detail="project not found")

    mode = data.get("mode", "normal")
    agent_id = str(uuid.uuid4())[:8]

    agents[agent_id] = {
        "project": project,
        "project_path": project_path,
        "session_id": None,
        "status": "idle",
        "process": None,
        "mode": mode,
        "messages": [],  # Store message history for persistence
        "waiting_on_user": False,  # True when agent finished turn, waiting for user
    }

    # Initialize empty auto-approve patterns for this agent
    auto_approve_patterns[agent_id] = set()

    # Broadcast init so frontend knows about the agent before output arrives
    await broadcast({
        "agent_id": agent_id,
        "type": "init",
        "project": project,
        "status": "idle",
        "mode": mode,
    })

    initial_prompt = (
        "Generate an Executive Summary of this project.\n\n"
        "First, run these git commands:\n"
        "- git log --oneline -10\n"
        "- git status\n"
        "- git diff --stat (if uncommitted changes exist)\n\n"
        "REPORT FORMAT:\n"
        "1. RECENT COMMITS: Summarize what was done in the last few commits\n"
        "2. CURRENT STATE: Branch name, uncommitted changes, any issues\n"
        "3. NEXT STEPS: Top 3 suggested priorities based on the code\n\n"
        "Be concise. Then wait for instructions."
    )
    asyncio.create_task(run_claude(agent_id, initial_prompt, project_path, mode=mode))

    return {"agent_id": agent_id, "project": project, "mode": mode}


@app.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str):
    """Delete an agent."""
    agent = agents.pop(agent_id, None)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")

    # Kill process if running
    if agent.get("process"):
        agent["process"].terminate()

    # Clean up auto-approve patterns
    auto_approve_patterns.pop(agent_id, None)

    await broadcast({"agent_id": agent_id, "type": "deleted"})
    return {"ok": True}


@app.post("/agents/{agent_id}/execute")
async def execute_plan(agent_id: str):
    """Switch agent from plan mode to execution mode."""
    agent = agents.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")

    session_id = agent.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="no session to resume")

    if agent.get("process"):
        agent["process"].terminate()
        await agent["process"].wait()

    # Switch to normal mode for execution
    agent["mode"] = "normal"

    asyncio.create_task(
        run_claude(
            agent_id,
            "Execute the plan.",
            agent["project_path"],
            session_id=session_id,
            mode="normal",
        )
    )

    return {"ok": True}


@app.get("/agents/{agent_id}/diff")
async def get_agent_diff(agent_id: str):
    """Get git diff for an agent's project."""
    agent = agents.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")

    project_path = agent["project_path"]
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        stat = result.stdout

        result = subprocess.run(
            ["git", "diff"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        diff = result.stdout

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = result.stdout

        return {"stat": stat, "diff": diff, "status": status}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# INTERNAL APPROVAL ENDPOINT
# =============================================================================


@app.post("/internal/approve-request")
async def handle_approve_request(request: Request):
    """
    Handle approval requests from MCP permission server.

    === CRITICAL: PERMISSION FLOW ===

    This endpoint is called by mcp_permission_server.py when Claude
    wants to use a tool that requires permission.

    Flow:
    1. MCP server POSTs here with {agent_id, tool_name, input, cwd}
    2. We check auto_approve_patterns for a matching pattern
    3. If no match, we broadcast to WebSocket and wait for user response
    4. User clicks Yes/No in browser, browser sends approval_response
    5. WebSocket handler resolves the Future
    6. We return the decision to MCP server
    7. MCP server returns to Claude CLI

    This endpoint BLOCKS until user responds (or pattern matches).
    The MCP server is designed to wait for our response.

    === END CRITICAL: PERMISSION FLOW ===
    """
    data = await request.json()
    agent_id = data.get("agent_id", "")
    tool_name = data.get("tool_name", "")
    tool_input = data.get("input", {})
    cwd = data.get("cwd", "")

    print(f"[Approval] Request from agent {agent_id}: {tool_name}", file=sys.stderr)

    # Check auto-approve patterns first
    if should_auto_approve(agent_id, tool_name, tool_input):
        print("[Approval] Auto-approved by pattern", file=sys.stderr)
        return {"behavior": "allow", "updatedInput": tool_input}

    # Need user approval - create request and wait
    request_id = str(uuid.uuid4())
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    pending_approvals[request_id] = future

    # Extract display info for UI
    command_display = ""
    if tool_name == "Bash":
        command_display = tool_input.get("command", "")
    elif tool_name in ("Edit", "Write", "MultiEdit"):
        command_display = tool_input.get("file_path", "") or tool_input.get(
            "filePath", ""
        )

    # Broadcast to UI
    await broadcast(
        {
            "type": "approval_request",
            "request_id": request_id,
            "agent_id": agent_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "command_display": command_display,
            "cwd": cwd,
            "pattern": extract_command_prefix(tool_name, tool_input),
        }
    )

    print(
        f"[Approval] Waiting for user response (request_id: {request_id})",
        file=sys.stderr,
    )

    # Wait for user response (blocking)
    try:
        decision = await future
    finally:
        pending_approvals.pop(request_id, None)

    print(f"[Approval] User decision: {decision}", file=sys.stderr)
    return decision


# =============================================================================
# WEBSOCKET HANDLER
# =============================================================================


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket for real-time agent communication.

    Incoming message types:
    - {agent_id, content}: Send message to agent
    - {type: "approval_response", request_id, decision, pattern?}: Approval response
    """
    await websocket.accept()
    clients.append(websocket)

    # Send current agent state
    for agent_id, agent in agents.items():
        await websocket.send_json(
            {
                "agent_id": agent_id,
                "type": "init",
                "project": agent["project"],
                "status": agent["status"],
                "mode": agent.get("mode", "normal"),
                "messages": agent.get("messages", []),
                "waiting_on_user": agent.get("waiting_on_user", False),
            }
        )

    try:
        while True:
            data = await websocket.receive_json()

            # === CRITICAL: APPROVAL RESPONSE HANDLING ===
            if data.get("type") == "approval_response":
                request_id = data.get("request_id")
                decision = data.get("decision")  # "allow" or "deny"
                pattern = data.get("pattern")  # Optional: pattern for "Yes to all X"
                agent_id = data.get("agent_id")

                print(
                    f"[WebSocket] Approval response: {decision} (pattern: {pattern})",
                    file=sys.stderr,
                )

                # Store pattern if provided (for "Yes to all X")
                if pattern and agent_id and decision == "allow":
                    # Use setdefault for atomic get-or-create
                    auto_approve_patterns.setdefault(agent_id, set()).add(pattern)
                    print(
                        f"[WebSocket] Added auto-approve pattern: {pattern}",
                        file=sys.stderr,
                    )

                # Resolve the pending approval future
                if request_id in pending_approvals:
                    future = pending_approvals[request_id]
                    if decision == "allow":
                        future.set_result(
                            {
                                "behavior": "allow",
                                "updatedInput": data.get("tool_input"),
                            }
                        )
                    else:
                        future.set_result(
                            {
                                "behavior": "deny",
                                "message": "Denied by user",
                            }
                        )

                continue
            # === END CRITICAL: APPROVAL RESPONSE HANDLING ===

            # Handle stop agent request
            if data.get("type") == "stop_agent":
                agent_id = data.get("agent_id")
                agent = agents.get(agent_id)
                if agent and agent.get("process"):
                    agent["process"].terminate()
                    print(f"[WebSocket] Stopped agent {agent_id}", file=sys.stderr)
                continue

            # Regular message to agent
            agent_id = data.get("agent_id")
            content = data.get("content", "")

            agent = agents.get(agent_id)
            if not agent:
                await websocket.send_json(
                    {
                        "error": "agent not found",
                        "agent_id": agent_id,
                    }
                )
                continue

            process = agent.get("process")

            if process and process.returncode is None:
                # Agent is running, send input to stdin in stream-json format
                if process.stdin:
                    try:
                        user_msg = json.dumps({
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": content}]
                            }
                        })
                        # Store user message for persistence
                        agent["messages"].append({"role": "user", "content": content})
                        agent["waiting_on_user"] = False  # User responded
                        process.stdin.write((user_msg + "\n").encode())
                        await process.stdin.drain()
                        agent["status"] = "working"
                        await broadcast(
                            {
                                "agent_id": agent_id,
                                "type": "status",
                                "status": "working",
                            }
                        )
                    except Exception as e:
                        print(f"[WebSocket] stdin write failed: {e}", file=sys.stderr)
                continue

            # Agent not running, start new Claude process
            asyncio.create_task(
                run_claude(
                    agent_id,
                    content,
                    agent["project_path"],
                    agent.get("session_id"),
                    agent.get("mode", "normal"),
                )
            )

    except WebSocketDisconnect:
        clients.remove(websocket)


# =============================================================================
# STATIC FILES
# =============================================================================

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=CALLBACK_PORT)
