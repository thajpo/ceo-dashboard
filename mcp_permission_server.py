#!/usr/bin/env python3
"""
MCP Permission Server for CEO Dashboard

This server runs as a subprocess, communicating with Claude CLI via stdin/stdout
using the MCP (Model Context Protocol) JSON-RPC format.

Architecture (based on timoconnellaus/claude-code-discord-bot):
    Claude CLI <--stdin/stdout--> This Server <--HTTP--> main.py <--WebSocket--> Browser

When Claude wants to use a tool that needs approval, it calls our "approve" tool.
We POST to the main FastAPI app, which broadcasts to the browser and waits for
user response. The response flows back through this chain.

=== CRITICAL LOGIC - DO NOT MODIFY WITHOUT UNDERSTANDING ===

1. MCP JSON-RPC Protocol:
   - Each message is a single line of JSON
   - Request: {"jsonrpc": "2.0", "id": N, "method": "...", "params": {...}}
   - Response: {"jsonrpc": "2.0", "id": N, "result": {...}}
   - We must respond with the SAME id as the request

2. Tool Registration:
   - On "initialize" we return our capabilities
   - On "tools/list" we return our available tools
   - On "tools/call" we execute the tool (this is where approval happens)

3. Blocking Behavior:
   - When approval is needed, we POST to main app and BLOCK until response
   - This is intentional - Claude waits for our response before proceeding

=== END CRITICAL LOGIC ===
"""

import asyncio
import json
import sys
import httpx


# Configuration - passed as command line args
AGENT_ID: str = ""
CALLBACK_URL: str = ""
CWD: str = ""


# =============================================================================
# MCP PROTOCOL HANDLERS
# =============================================================================
# These handlers implement the MCP JSON-RPC protocol.
# Reference: https://modelcontextprotocol.io/docs


async def handle_initialize(request_id: int, params: dict) -> dict:
    """
    Handle MCP initialize request.
    Returns server capabilities - we only provide tools, no resources/prompts.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},  # We provide tools
            },
            "serverInfo": {
                "name": "ceo-dashboard-permissions",
                "version": "1.0.0",
            },
        },
    }


async def handle_tools_list(request_id: int) -> dict:
    """
    Handle tools/list request.
    We expose a single "approve" tool that Claude calls for permission checks.

    The tool schema matches what Claude Code expects for --permission-prompt-tool.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "tools": [
                {
                    "name": "approve",
                    "description": "Request permission to use a tool",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "tool_name": {
                                "type": "string",
                                "description": "The tool requesting permission",
                            },
                            "input": {
                                "type": "object",
                                "description": "The input for the tool",
                                "additionalProperties": True,
                            },
                        },
                        "required": ["tool_name", "input"],
                    },
                }
            ]
        },
    }


async def handle_tools_call(request_id: int, params: dict) -> dict:
    """
    Handle tools/call request - this is where permission approval happens.

    === CRITICAL LOGIC ===
    1. Extract tool_name and input from params
    2. POST to main app's /internal/approve-request endpoint
    3. BLOCK waiting for response (main app waits for user via WebSocket)
    4. Return the decision to Claude

    The response format must match Claude Code's expected PermissionDecision:
    {
        "behavior": "allow" | "deny",
        "updatedInput": {...} | undefined,
        "message": "..." | undefined
    }
    === END CRITICAL LOGIC ===
    """
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    if tool_name != "approve":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"Unknown tool: {tool_name}",
            },
        }

    # Extract the actual tool being approved and its input
    target_tool = arguments.get("tool_name", "unknown")
    target_input = arguments.get("input", {})

    # Log to stderr (stdout is reserved for MCP protocol)
    print(f"[MCP] Permission request: {target_tool}", file=sys.stderr)
    print(f"[MCP] Input: {json.dumps(target_input, indent=2)}", file=sys.stderr)

    try:
        # POST to main app and wait for response
        # This blocks until user approves/denies via WebSocket
        async with httpx.AsyncClient(
            timeout=None
        ) as client:  # No timeout - wait forever
            response = await client.post(
                CALLBACK_URL,
                json={
                    "agent_id": AGENT_ID,
                    "tool_name": target_tool,
                    "input": target_input,
                    "cwd": CWD,
                },
            )
            decision = response.json()

        print(f"[MCP] Decision: {decision}", file=sys.stderr)

        # Return decision as tool result
        # Claude expects the result content to be the permission decision JSON
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(decision),
                    }
                ],
            },
        }

    except Exception as e:
        print(f"[MCP] Error: {e}", file=sys.stderr)
        # On error, deny for safety
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "behavior": "deny",
                                "message": f"Permission server error: {e}",
                            }
                        ),
                    }
                ],
            },
        }


async def handle_notification(method: str, params: dict) -> None:
    """
    Handle MCP notifications (no response needed).
    Currently we just log them.
    """
    print(f"[MCP] Notification: {method} {params}", file=sys.stderr)


# =============================================================================
# MAIN MESSAGE ROUTER
# =============================================================================


async def handle_message(message: dict) -> dict | None:
    """
    Route incoming MCP messages to appropriate handlers.

    Returns None for notifications (no response needed).
    Returns dict for requests (must respond).
    """
    method = message.get("method", "")
    params = message.get("params", {})
    request_id = message.get("id")

    # Notifications have no id - don't respond
    if request_id is None:
        await handle_notification(method, params)
        return None

    # Route to appropriate handler
    if method == "initialize":
        return await handle_initialize(request_id, params)
    elif method == "notifications/initialized":
        # Client acknowledging initialization - no response needed but has id sometimes
        return None
    elif method == "tools/list":
        return await handle_tools_list(request_id)
    elif method == "tools/call":
        return await handle_tools_call(request_id, params)
    else:
        # Unknown method
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}",
            },
        }


# =============================================================================
# STDIN/STDOUT COMMUNICATION
# =============================================================================


async def run_server():
    """
    Main server loop - read from stdin, write to stdout.

    === CRITICAL LOGIC ===
    - Each line from stdin is a complete JSON-RPC message
    - Each line to stdout must be a complete JSON-RPC response
    - stdout is ONLY for MCP protocol - all logging goes to stderr
    === END CRITICAL LOGIC ===
    """
    print(f"[MCP] Server started for agent {AGENT_ID}", file=sys.stderr)
    print(f"[MCP] Callback URL: {CALLBACK_URL}", file=sys.stderr)

    # Use asyncio for non-blocking stdin reading
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        try:
            # Read a line from stdin
            line = await reader.readline()
            if not line:
                print("[MCP] EOF on stdin, exiting", file=sys.stderr)
                break

            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue

            print(f"[MCP] Received: {line_str[:200]}...", file=sys.stderr)

            # Parse JSON-RPC message
            try:
                message = json.loads(line_str)
            except json.JSONDecodeError as e:
                print(f"[MCP] JSON parse error: {e}", file=sys.stderr)
                continue

            # Handle message
            response = await handle_message(message)

            # Send response if needed
            if response is not None:
                response_str = json.dumps(response)
                print(response_str, flush=True)  # stdout for MCP
                print(f"[MCP] Sent: {response_str[:200]}...", file=sys.stderr)

        except Exception as e:
            print(f"[MCP] Error in main loop: {e}", file=sys.stderr)
            continue


def main():
    """
    Entry point - parse args and start server.

    Usage: python mcp_permission_server.py <agent_id> <callback_url> <cwd>
    """
    global AGENT_ID, CALLBACK_URL, CWD

    if len(sys.argv) < 4:
        print(
            "Usage: mcp_permission_server.py <agent_id> <callback_url> <cwd>",
            file=sys.stderr,
        )
        sys.exit(1)

    AGENT_ID = sys.argv[1]
    CALLBACK_URL = sys.argv[2]
    CWD = sys.argv[3]

    asyncio.run(run_server())


if __name__ == "__main__":
    main()
