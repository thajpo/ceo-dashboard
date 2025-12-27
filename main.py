import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

PROJECTS_DIR = Path.home() / "Projects"

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


async def run_claude(agent_id: str, prompt: str, project_path: Path, session_id: str | None = None, mode: str = "normal"):
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
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=project_path,
    )
    agent["process"] = process

    accumulated_text = ""

    async for line in process.stdout:
        line_str = line.decode("utf-8", errors="replace").strip()
        if not line_str:
            continue

        try:
            data = json.loads(line_str)
            msg_type = data.get("type", "")

            # Capture session_id from final result message
            if msg_type == "result" and "session_id" in data:
                agent["session_id"] = data["session_id"]

            # Detect interrupts (questions, approvals, plan mode)
            is_interrupt = False
            interrupt_type = None  # question, approval, plan

            # Check for assistant messages
            if msg_type == "assistant":
                message = data.get("message", {})
                content_blocks = message.get("content", [])
                for block in content_blocks:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        accumulated_text += text
                        # Check for questions
                        if "?" in text and any(q in text.lower() for q in ["would you", "should i", "do you want", "which", "what", "how"]):
                            is_interrupt = True
                            interrupt_type = "question"
                    # Check for tool use that needs attention
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "")
                        if tool_name in ("AskUserQuestion", "AskHuman"):
                            is_interrupt = True
                            interrupt_type = "question"
                        elif tool_name == "ExitPlanMode":
                            is_interrupt = True
                            interrupt_type = "plan"
                        elif tool_name in ("Edit", "Write", "Bash", "NotebookEdit"):
                            # These might need approval in normal mode
                            is_interrupt = True
                            interrupt_type = "approval"

            await broadcast({
                "agent_id": agent_id,
                "type": "interrupt" if is_interrupt else "output",
                "interrupt_type": interrupt_type,
                "content": data,
                "raw": line_str,
            })

            if is_interrupt:
                agent["status"] = "needs_attention"
                await broadcast({"agent_id": agent_id, "type": "status", "status": "needs_attention"})

        except json.JSONDecodeError:
            # Non-JSON output, send as raw
            await broadcast({
                "agent_id": agent_id,
                "type": "output",
                "content": {"raw": line_str},
                "raw": line_str,
            })

    await process.wait()
    agent["process"] = None
    agent["status"] = "idle"
    await broadcast({"agent_id": agent_id, "type": "status", "status": "idle"})


@app.get("/projects")
async def list_projects():
    """List all directories in ~/Projects/."""
    if not PROJECTS_DIR.exists():
        return {"projects": []}

    projects = [
        d.name for d in PROJECTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ]
    return {"projects": sorted(projects)}


@app.post("/agents")
async def create_agent(data: dict):
    """Create a new agent for a project."""
    project = data.get("project")
    if not project:
        return {"error": "project required"}, 400

    project_path = PROJECTS_DIR / project
    if not project_path.exists():
        return {"error": "project not found"}, 404

    # Options from request
    mode = data.get("mode", "normal")  # normal, plan, yolo

    agent_id = str(uuid.uuid4())[:8]
    agents[agent_id] = {
        "project": project,
        "project_path": project_path,
        "session_id": None,
        "status": "idle",
        "process": None,
        "mode": mode,
    }

    # Start with initial prompt
    initial_prompt = "Give me a one-line repo status. Format: 'branch: status. Recent: brief summary of last 2-3 commits'. No markdown, no tables, no headers. Be terse. Then wait."
    asyncio.create_task(run_claude(agent_id, initial_prompt, project_path, mode=mode))

    return {"agent_id": agent_id, "project": project, "mode": mode}


@app.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str):
    """Delete an agent."""
    agent = agents.pop(agent_id, None)
    if not agent:
        return {"error": "agent not found"}, 404

    # Kill process if running
    if agent.get("process"):
        agent["process"].terminate()

    await broadcast({"agent_id": agent_id, "type": "deleted"})
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time agent communication."""
    await websocket.accept()
    clients.append(websocket)

    # Send current agent states
    for agent_id, agent in agents.items():
        await websocket.send_json({
            "agent_id": agent_id,
            "type": "init",
            "project": agent["project"],
            "status": agent["status"],
        })

    try:
        while True:
            data = await websocket.receive_json()
            agent_id = data.get("agent_id")
            content = data.get("content", "")

            agent = agents.get(agent_id)
            if not agent:
                await websocket.send_json({"error": "agent not found", "agent_id": agent_id})
                continue

            # Don't send if already processing
            if agent["status"] == "working":
                await websocket.send_json({"error": "agent busy", "agent_id": agent_id})
                continue

            # Run claude with the user's message
            asyncio.create_task(run_claude(
                agent_id,
                content,
                agent["project_path"],
                agent.get("session_id"),
                agent.get("mode", "normal"),
            ))

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
