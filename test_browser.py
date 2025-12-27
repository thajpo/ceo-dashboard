"""
Browser testing script for CEO Dashboard.
Uses Playwright to test UI interactions and capture screenshots for analysis.
"""

import asyncio
import subprocess
import time
from pathlib import Path

from playwright.async_api import async_playwright


SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)


async def test_dashboard():
    """Test the CEO Dashboard UI."""

    # Start the server
    print("Starting server...")
    server = subprocess.Popen(
        ["uv", "run", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8765"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=Path(__file__).parent,
    )

    # Wait for server to start
    time.sleep(2)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1400, "height": 900})

            # Collect console messages
            console_messages = []
            page.on(
                "console",
                lambda msg: console_messages.append(f"{msg.type}: {msg.text}"),
            )

            # Collect errors
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))

            print("Loading dashboard...")
            await page.goto("http://127.0.0.1:8765")
            await page.wait_for_load_state("networkidle")

            # Screenshot: Initial state (inbox view)
            await page.screenshot(path=SCREENSHOTS_DIR / "01_inbox_empty.png")
            print("Screenshot saved: 01_inbox_empty.png")

            # Wait for WebSocket connection
            await asyncio.sleep(1)

            # Click "+ Add Project" button to open modal
            print("Opening project modal...")
            await page.click("text=+ Add Project")
            await asyncio.sleep(0.5)

            # Screenshot: Project selection modal
            await page.screenshot(path=SCREENSHOTS_DIR / "02_project_modal.png")
            print("Screenshot saved: 02_project_modal.png")

            # Check for projects in modal
            projects = await page.locator(".project-option").all()
            print(f"Found {len(projects)} projects in modal")
            inbox_items = []

            if projects:
                # Click first project
                first_project = projects[0]
                project_name = await first_project.locator(
                    ".project-option-name"
                ).text_content()
                print(f"Selecting project: {project_name}")
                await first_project.click()

                # Wait for agent to start and respond
                print("Waiting for Claude response...")
                await asyncio.sleep(5)

                # Screenshot: Inbox with item
                await page.screenshot(path=SCREENSHOTS_DIR / "03_inbox_with_item.png")
                print("Screenshot saved: 03_inbox_with_item.png")

                # Check for inbox items
                inbox_items = await page.locator(".inbox-item").all()
                print(f"Found {len(inbox_items)} inbox items")

                # Check for working agents
                working_agents = await page.locator(".working-agent").all()
                print(f"Found {len(working_agents)} working agents")

                # If there's an inbox item, click it to open conversation
                if inbox_items:
                    print("Opening conversation view...")
                    await inbox_items[0].click()
                    await asyncio.sleep(0.5)

                    # Screenshot: Conversation view
                    await page.screenshot(path=SCREENSHOTS_DIR / "04_conversation.png")
                    print("Screenshot saved: 04_conversation.png")

                    # Check for messages
                    messages = await page.locator(".message").all()
                    print(f"Found {len(messages)} messages in conversation")

                    # Try typing a response
                    await page.fill(
                        "#conversation-input",
                        "Use PostgreSQL, it's already in the stack",
                    )
                    await page.screenshot(
                        path=SCREENSHOTS_DIR / "05_typing_response.png"
                    )
                    print("Screenshot saved: 05_typing_response.png")

                    # Send the message (this should auto-return to inbox)
                    await page.click("text=Send")
                    await asyncio.sleep(0.5)

                    # Screenshot: After sending (should be back at inbox)
                    await page.screenshot(path=SCREENSHOTS_DIR / "06_after_send.png")
                    print("Screenshot saved: 06_after_send.png")

            # Report console messages
            if console_messages:
                print("\n--- Console Messages ---")
                for msg in console_messages[-10:]:
                    print(f"  {msg}")

            # Report errors
            if errors:
                print("\n--- Errors ---")
                for err in errors:
                    print(f"  {err}")

            await browser.close()

            inbox_count = 0
            if projects:
                try:
                    inbox_count = len(inbox_items)
                except NameError:
                    pass

            return {
                "projects_found": len(projects),
                "inbox_items": inbox_count,
                "console_messages": console_messages,
                "errors": errors,
            }

    finally:
        print("\nStopping server...")
        server.terminate()
        server.wait()


if __name__ == "__main__":
    result = asyncio.run(test_dashboard())
    print("\n=== Test Complete ===")
    print(f"Projects: {result['projects_found']}")
    print(f"Inbox items: {result.get('inbox_items', 0)}")
    print(f"Errors: {len(result['errors'])}")
