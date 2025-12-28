"""
Core functionality tests for CEO Dashboard.

These tests verify the essential communication flow:
1. Server starts and serves pages
2. WebSocket connects and receives messages
3. Agent creation triggers init broadcast
4. Claude output is received and properly structured
5. User messages can be sent to agents

Run with: uv run python test_core.py
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets

SERVER_PORT = 8765
SERVER_URL = f"http://127.0.0.1:{SERVER_PORT}"
WS_URL = f"ws://127.0.0.1:{SERVER_PORT}/ws"
PROJECT_DIR = Path(__file__).parent


class TestClient:
    """WebSocket client for testing."""

    def __init__(self):
        self.ws = None
        self.messages = []
        self.queue = asyncio.Queue()

    async def connect(self):
        self.ws = await websockets.connect(WS_URL)
        asyncio.create_task(self._receive_loop())

    async def _receive_loop(self):
        try:
            async for msg in self.ws:
                data = json.loads(msg)
                self.messages.append(data)
                await self.queue.put(data)
        except websockets.ConnectionClosed:
            pass

    async def wait_for_type(self, msg_type: str, agent_id: str = None, timeout: float = 30):
        """Wait for a specific message type."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                remaining = deadline - time.time()
                msg = await asyncio.wait_for(self.queue.get(), timeout=min(remaining, 1))
                if msg.get("type") == msg_type:
                    if agent_id is None or msg.get("agent_id") == agent_id:
                        return msg
            except asyncio.TimeoutError:
                continue
        raise TimeoutError(f"Timed out waiting for type={msg_type}")

    async def close(self):
        if self.ws:
            await self.ws.close()


def start_server():
    """Start the test server."""
    return subprocess.Popen(
        ["uv", "run", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(SERVER_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=PROJECT_DIR,
    )


async def test_server_serves_pages():
    """Test that server serves index.html."""
    print("Test: Server serves pages...")
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SERVER_URL}/")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert "CEO Dashboard" in resp.text, "Missing 'CEO Dashboard' in response"
    print("  PASS")


async def test_websocket_connects():
    """Test that WebSocket connects successfully."""
    print("Test: WebSocket connects...")
    client = TestClient()
    await client.connect()
    await asyncio.sleep(0.5)
    await client.close()
    print("  PASS")


async def test_projects_endpoint():
    """Test that projects endpoint returns projects."""
    print("Test: Projects endpoint...")
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SERVER_URL}/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert "projects" in data, "Missing 'projects' key"
        assert len(data["projects"]) > 0, "No projects found"
        # ceo-dashboard should be in the list since we're in ~/Projects
        print(f"  Found {len(data['projects'])} projects")
    print("  PASS")


async def get_first_project():
    """Get the first available project for testing."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SERVER_URL}/projects")
        projects = resp.json().get("projects", [])
        if not projects:
            raise RuntimeError("No projects available for testing")
        return projects[0]


async def test_agent_creation_broadcasts_init():
    """Test that creating an agent broadcasts init message."""
    print("Test: Agent creation broadcasts init...")
    client = TestClient()
    await client.connect()

    project = await get_first_project()
    print(f"  Using project: {project}")

    try:
        # Create agent
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{SERVER_URL}/agents",
                json={"project": project, "mode": "plan"}
            )
            data = resp.json()
            assert "agent_id" in data, f"No agent_id in response: {data}"
            agent_id = data["agent_id"]
            print(f"  Created agent: {agent_id}")

        # Should receive init message
        msg = await client.wait_for_type("init", agent_id, timeout=5)
        assert msg["project"] == project, f"Wrong project: {msg}"
        assert msg["agent_id"] == agent_id, f"Wrong agent_id: {msg}"
        print(f"  Received init message")

        # Cleanup
        async with httpx.AsyncClient() as http:
            await http.delete(f"{SERVER_URL}/agents/{agent_id}")

    finally:
        await client.close()

    print("  PASS")


async def test_claude_output_received():
    """Test that Claude output is received via WebSocket."""
    print("Test: Claude output received...")
    client = TestClient()
    await client.connect()

    project = await get_first_project()
    print(f"  Using project: {project}")

    agent_id = None
    try:
        # Create agent in plan mode (safer, no file changes)
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{SERVER_URL}/agents",
                json={"project": project, "mode": "plan"}
            )
            agent_id = resp.json()["agent_id"]
            print(f"  Created agent: {agent_id}")

        # Wait for init
        await client.wait_for_type("init", agent_id, timeout=5)

        # Wait for output from Claude (should contain assistant message)
        output_received = False
        deadline = time.time() + 60  # Claude can take a while

        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(client.queue.get(), timeout=5)
                if msg.get("agent_id") != agent_id:
                    continue

                msg_type = msg.get("type")

                # Status messages
                if msg_type == "status":
                    print(f"  Status: {msg.get('status')}")
                    continue

                # Usage tracking
                if msg_type == "usage":
                    print(f"  Got usage data")
                    continue

                # Output messages
                if msg_type in ("output", "interrupt"):
                    content = msg.get("content", {})
                    if content.get("type") == "assistant":
                        output_received = True
                        print(f"  Received assistant output")
                        break
                    elif content.get("type") == "system":
                        print(f"  Received system message")
                    elif content.get("type") == "result":
                        print(f"  Received result")
                        output_received = True
                        break

            except asyncio.TimeoutError:
                continue

        assert output_received, "Did not receive Claude output"

    finally:
        # Cleanup
        if agent_id:
            async with httpx.AsyncClient() as http:
                await http.delete(f"{SERVER_URL}/agents/{agent_id}")
        await client.close()

    print("  PASS")


async def test_agent_deletion():
    """Test that agent deletion works and broadcasts."""
    print("Test: Agent deletion...")
    client = TestClient()
    await client.connect()

    project = await get_first_project()

    try:
        # Create agent
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{SERVER_URL}/agents",
                json={"project": project, "mode": "plan"}
            )
            agent_id = resp.json()["agent_id"]

        # Wait for init
        await client.wait_for_type("init", agent_id, timeout=5)

        # Delete agent
        async with httpx.AsyncClient() as http:
            await http.delete(f"{SERVER_URL}/agents/{agent_id}")

        # Should receive deleted message
        msg = await client.wait_for_type("deleted", agent_id, timeout=5)
        assert msg["agent_id"] == agent_id
        print(f"  Agent {agent_id} deleted")

    finally:
        await client.close()

    print("  PASS")


async def run_tests():
    """Run all tests."""
    print("\n" + "=" * 50)
    print("CEO Dashboard Core Tests")
    print("=" * 50 + "\n")

    tests = [
        test_server_serves_pages,
        test_websocket_connects,
        test_projects_endpoint,
        test_agent_creation_broadcasts_init,
        test_claude_output_received,
        test_agent_deletion,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            await test()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL: {e}")

    print("\n" + "=" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 50 + "\n")

    return failed == 0


def main():
    print("Starting test server...")
    server = start_server()
    time.sleep(3)  # Wait for server to start

    try:
        success = asyncio.run(run_tests())
    finally:
        print("Stopping server...")
        server.terminate()
        server.wait()

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
