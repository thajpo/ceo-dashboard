"""
End-to-end tests for CEO Dashboard using real Claude CLI.
"""

import asyncio
import json
import subprocess
import time
from pathlib import Path

import httpx
import websockets


SERVER_PORT = 8766
SERVER_URL = f"http://127.0.0.1:{SERVER_PORT}"
WS_URL = f"ws://127.0.0.1:{SERVER_PORT}/ws"
PROJECT_DIR = Path(__file__).parent
TEST_FILE = PROJECT_DIR / "test-dummy.txt"


class DashboardTestClient:
    def __init__(self):
        self.ws = None
        self.messages = []
        self.message_queue = asyncio.Queue()

    async def connect(self):
        self.ws = await websockets.connect(WS_URL)
        asyncio.create_task(self._receive_loop())

    async def _receive_loop(self):
        if not self.ws:
            return
        try:
            async for message in self.ws:
                data = json.loads(message)
                self.messages.append(data)
                await self.message_queue.put(data)
        except websockets.ConnectionClosed:
            pass

    async def send(self, agent_id, content):
        if not self.ws:
            raise RuntimeError("WebSocket not connected")
        await self.ws.send(json.dumps({"agent_id": agent_id, "content": content}))

    async def wait_for(
        self,
        agent_id,
        msg_type=None,
        interrupt_type=None,
        timeout=60,
    ):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                remaining = deadline - time.time()
                msg = await asyncio.wait_for(
                    self.message_queue.get(), timeout=min(remaining, 1)
                )
                if msg.get("agent_id") != agent_id:
                    continue
                if msg_type and msg.get("type") != msg_type:
                    continue
                if interrupt_type and msg.get("interrupt_type") != interrupt_type:
                    continue
                return msg
            except asyncio.TimeoutError:
                continue
        raise TimeoutError(f"Timed out waiting for {msg_type}/{interrupt_type}")

    async def wait_for_status(self, agent_id: str, status: str, timeout: float = 60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                remaining = deadline - time.time()
                msg = await asyncio.wait_for(
                    self.message_queue.get(), timeout=min(remaining, 1)
                )
                if msg.get("agent_id") != agent_id:
                    continue
                if msg.get("type") == "status" and msg.get("status") == status:
                    return msg
            except asyncio.TimeoutError:
                continue
        raise TimeoutError(f"Timed out waiting for status={status}")

    async def create_agent(self, project: str, mode: str = "normal") -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{SERVER_URL}/agents",
                json={"project": project, "mode": mode},
            )
            data = resp.json()
            if "agent_id" not in data:
                raise RuntimeError(f"Failed to create agent: {data}")
            return data["agent_id"]

    async def delete_agent(self, agent_id: str):
        async with httpx.AsyncClient() as client:
            await client.delete(f"{SERVER_URL}/agents/{agent_id}")

    async def get_diff(self, agent_id: str):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{SERVER_URL}/agents/{agent_id}/diff")
            return resp.json()

    async def close(self):
        if self.ws:
            await self.ws.close()


def start_server():
    return subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(SERVER_PORT),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=PROJECT_DIR,
    )


def cleanup_test_file():
    if TEST_FILE.exists():
        TEST_FILE.unlink()


async def test_server_starts():
    print("Testing: Server starts and serves index.html...")
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SERVER_URL}/")
        assert resp.status_code == 200
        assert "CEO Dashboard" in resp.text
    print("  PASS")


async def test_websocket_connects():
    print("Testing: WebSocket connects...")
    client = DashboardTestClient()
    await client.connect()
    await asyncio.sleep(0.5)
    await client.close()
    print("  PASS")


async def test_projects_listed():
    print("Testing: Projects endpoint returns ceo-dashboard...")
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SERVER_URL}/projects")
        data = resp.json()
        assert "ceo-dashboard" in data["projects"], f"Projects: {data['projects']}"
    print("  PASS")


async def test_agent_spawn_initial_status():
    print("Testing: Agent spawn and initial status report...")
    client = DashboardTestClient()
    await client.connect()

    agent_id = await client.create_agent("ceo-dashboard", mode="normal")
    print(f"  Created agent: {agent_id}")

    try:
        msg = await client.wait_for(agent_id, msg_type="interrupt", timeout=90)
        content = str(msg.get("content", {}))
        print(f"  Got interrupt: {msg.get('interrupt_type')}")
        assert msg.get("interrupt_type") in ("complete", "status", "approval"), (
            f"Unexpected: {msg}"
        )
    finally:
        await client.delete_agent(agent_id)
        await client.close()
    print("  PASS")


async def test_file_creation_approval_flow():
    print("Testing: File creation with approval flow...")
    cleanup_test_file()
    client = DashboardTestClient()
    await client.connect()

    agent_id = await client.create_agent("ceo-dashboard", mode="normal")
    print(f"  Created agent: {agent_id}")

    try:
        print("  Waiting for initial status...")
        await client.wait_for(agent_id, msg_type="interrupt", timeout=90)

        print("  Sending file creation instruction...")
        await client.send(
            agent_id,
            "Create a file called test-dummy.txt with the exact content 'e2e test'. Use the Write tool, not Bash.",
        )

        print("  Waiting for completion...")
        await client.wait_for_status(agent_id, "idle", timeout=90)

        assert TEST_FILE.exists(), "test-dummy.txt was not created"
        content = TEST_FILE.read_text().strip()
        assert content == "e2e test", f"File content wrong: {content}"
        print("  File created with correct content")
    finally:
        cleanup_test_file()
        await client.delete_agent(agent_id)
        await client.close()
    print("  PASS")


async def test_bash_approval_flow():
    print("Testing: Bash command requires approval...")
    cleanup_test_file()
    client = DashboardTestClient()
    await client.connect()

    agent_id = await client.create_agent("ceo-dashboard", mode="normal")
    print(f"  Created agent: {agent_id}")

    try:
        print("  Waiting for initial status...")
        await client.wait_for(agent_id, msg_type="interrupt", timeout=90)

        print("  Sending bash instruction...")
        await client.send(
            agent_id,
            "Run this exact bash command: echo 'e2e test' > test-dummy.txt",
        )

        print("  Waiting for approval interrupt...")
        msg = await client.wait_for(
            agent_id, msg_type="interrupt", interrupt_type="approval", timeout=60
        )
        print(f"  Got approval interrupt")

        print("  Approving...")
        await client.send(agent_id, "y")

        print("  Waiting for completion...")
        await client.wait_for_status(agent_id, "idle", timeout=60)

        assert TEST_FILE.exists(), "test-dummy.txt was not created"
        content = TEST_FILE.read_text().strip()
        assert content == "e2e test", f"File content wrong: {content}"
        print("  Bash command executed after approval")
    finally:
        cleanup_test_file()
        await client.delete_agent(agent_id)
        await client.close()
    print("  PASS")


async def test_diff_view():
    print("Testing: Diff view endpoint...")
    client = DashboardTestClient()
    await client.connect()

    agent_id = await client.create_agent("ceo-dashboard", mode="normal")

    try:
        diff_data = await client.get_diff(agent_id)
        assert "diff" in diff_data
        assert "stat" in diff_data
        assert "status" in diff_data
        print(f"  Diff endpoint returned keys: {list(diff_data.keys())}")
    finally:
        await client.delete_agent(agent_id)
        await client.close()
    print("  PASS")


async def test_usage_tracking():
    print("Testing: Usage tracking...")
    client = DashboardTestClient()
    await client.connect()

    agent_id = await client.create_agent("ceo-dashboard", mode="normal")

    try:
        print("  Waiting for usage data...")
        msg = await client.wait_for(agent_id, msg_type="usage", timeout=90)
        usage = msg.get("usage", {})
        assert "input_tokens" in usage or "output_tokens" in usage, (
            f"No tokens in usage: {usage}"
        )
        print(f"  Got usage: {usage}")
    finally:
        await client.delete_agent(agent_id)
        await client.close()
    print("  PASS")


async def test_agent_deletion():
    print("Testing: Agent deletion...")
    client = DashboardTestClient()
    await client.connect()

    agent_id = await client.create_agent("ceo-dashboard", mode="normal")
    print(f"  Created agent: {agent_id}")

    await client.delete_agent(agent_id)

    msg = await client.wait_for(agent_id, msg_type="deleted", timeout=5)
    assert msg.get("type") == "deleted"
    print("  Agent deleted successfully")

    async with httpx.AsyncClient() as http:
        resp = await http.get(f"{SERVER_URL}/agents/{agent_id}/diff")
        assert resp.status_code == 404

    await client.close()
    print("  PASS")


async def test_plan_mode():
    print("Testing: Plan mode...")
    client = DashboardTestClient()
    await client.connect()

    agent_id = await client.create_agent("ceo-dashboard", mode="plan")
    print(f"  Created agent in plan mode: {agent_id}")

    try:
        print("  Waiting for plan output...")
        msg = await client.wait_for(agent_id, msg_type="interrupt", timeout=90)
        print(f"  Got interrupt type: {msg.get('interrupt_type')}")
    finally:
        await client.delete_agent(agent_id)
        await client.close()
    print("  PASS")


async def run_all_tests():
    print("\n" + "=" * 60)
    print("CEO Dashboard E2E Tests")
    print("=" * 60 + "\n")

    tests = [
        test_server_starts,
        test_websocket_connects,
        test_projects_listed,
        test_agent_spawn_initial_status,
        test_usage_tracking,
        test_diff_view,
        test_agent_deletion,
        test_file_creation_approval_flow,
        test_bash_approval_flow,
        test_plan_mode,
    ]

    passed = 0
    failed = 0
    errors = []

    for test in tests:
        try:
            await test()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((test.__name__, str(e)))
            print(f"  FAIL: {e}")

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for name, error in errors:
            print(f"  {name}: {error}")
    print("=" * 60 + "\n")

    return failed == 0


def main():
    cleanup_test_file()
    print("Starting server...")
    server = start_server()
    time.sleep(3)

    try:
        success = asyncio.run(run_all_tests())
    finally:
        print("Stopping server...")
        server.terminate()
        server.wait()
        cleanup_test_file()

    return 0 if success else 1


if __name__ == "__main__":
    exit(main())
