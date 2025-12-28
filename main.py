import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Default to ~/Projects, override with CEO_PROJECTS_DIR env var
PROJECTS_DIR = Path(os.environ.get("CEO_PROJECTS_DIR", Path.home() / "Projects"))

# Agent state: agent_id -> {project, session_id, status, process}
agents: dict[str, dict[str, Any]] = {}

# Connected WebSocket clients
clients: list[WebSocket] = []


async def broadcast(message: dict):
    """Send message to all connected clients."""
    dead = []
    for client in clients:
        try:
            await client.send_json(message)
        except Exception:
            dead.append(client)
    for client in dead:
        clients.remove(client)


async def run_claude(
    agent_id: str,
    prompt: str,
    project_path: Path,
    session_id: str | None = None,
    mode: str = "normal",
):
    """Run claude CLI and stream output."""
    agent = agents.get(agent_id)
    if not agent:
        return

    agent["status"] = "working"
    await broadcast({"agent_id": agent_id, "type": "status", "status": "working"})

    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]

    # Mode-specific flags
    if mode == "plan":
        cmd.extend(["--permission-mode", "plan"])
    elif mode == "yolo":
        cmd.append("--dangerously-skip-permissions")

    if session_id:
        cmd.extend(["--resume", session_id])

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=project_path,
    )
    agent["process"] = process

    accumulated_text = ""
    has_interrupt = False

    if process.stdout is None:
        return

    async for line in process.stdout:
        line_str = line.decode("utf-8", errors="replace").strip()
        if not line_str:
            continue

        try:
            data = json.loads(line_str)
            msg_type = data.get("type", "")

            # Broadcast usage if present
            if "usage" in data:
                await broadcast(
                    {"agent_id": agent_id, "type": "usage", "usage": data["usage"]}
                )

            # Capture session_id from final result message
            if msg_type == "result" and "session_id" in data:
                agent["session_id"] = data["session_id"]

            is_interrupt = False
            interrupt_type = None

            # Check for assistant messages
            if msg_type == "assistant":
                message = data.get("message", {})
                content_blocks = message.get("content", [])
                for block in content_blocks:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        accumulated_text += text
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
                    # Check for tool use that needs attention
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "")

                        # Broadcast tool usage for UI
                        await broadcast(
                            {
                                "agent_id": agent_id,
                                "type": "tool",
                                "tool": {
                                    "name": tool_name,
                                    "input": block.get("input", {}),
                                },
                            }
                        )

                        if tool_name in ("AskUserQuestion", "AskHuman"):
                            is_interrupt = True
                            interrupt_type = "question"
                        elif tool_name == "ExitPlanMode":
                            is_interrupt = True
                            interrupt_type = "plan"
                        elif tool_name in ("Edit", "Write", "NotebookEdit"):
                            if process.stdin:
                                process.stdin.write(b"y\n")
                                await process.stdin.drain()
                        elif tool_name == "Bash":
                            is_interrupt = True
                            interrupt_type = "approval"

            if is_interrupt:
                has_interrupt = True

            await broadcast(
                {
                    "agent_id": agent_id,
                    "type": "interrupt" if is_interrupt else "output",
                    "interrupt_type": interrupt_type,
                    "content": data,
                    "raw": line_str,
                }
            )

            if is_interrupt:
                agent["status"] = "needs_attention"
                await broadcast(
                    {
                        "agent_id": agent_id,
                        "type": "status",
                        "status": "needs_attention",
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

    await process.wait()
    agent["process"] = None
    agent["status"] = "idle"

    if accumulated_text and not has_interrupt:
        await broadcast(
            {
                "agent_id": agent_id,
                "type": "interrupt",
                "interrupt_type": "complete",
                "content": {"type": "completion", "text": accumulated_text},
            }
        )

    await broadcast({"agent_id": agent_id, "type": "status", "status": "idle"})


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
    }

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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time agent communication."""
    await websocket.accept()
    clients.append(websocket)

    for agent_id, agent in agents.items():
        await websocket.send_json(
            {
                "agent_id": agent_id,
                "type": "init",
                "project": agent["project"],
                "status": agent["status"],
                "mode": agent.get("mode", "normal"),
            }
        )

    try:
        while True:
            data = await websocket.receive_json()
            agent_id = data.get("agent_id")
            content = data.get("content", "")

            agent = agents.get(agent_id)
            if not agent:
                await websocket.send_json(
                    {"error": "agent not found", "agent_id": agent_id}
                )
                continue

            process = agent.get("process")

            if process and process.returncode is None:
                if process.stdin:
                    try:
                        process.stdin.write((content + "\n").encode())
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
                        print(f"stdin write failed: {e}")
                continue

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


# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
