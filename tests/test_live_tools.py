"""
Live integration test for all kubernetes-mcp tools.

Uses the MCP streamable-http transport to call every tool on the server
against a kubectl proxy target cluster.

Run:
  1. kubectl proxy --port=8001
  2. Start MCP server with .env configured for proxy_url
  3. PYTHONPATH=. python tests/test_live_tools.py
"""

from __future__ import annotations

import json
import sys
import httpx

MCP_URL = "http://localhost:8080/mcp"
CLUSTER = "local"

# MCP uses JSON-RPC 2.0 over streamable-http
_req_id = 0


def _next_id() -> int:
    global _req_id
    _req_id += 1
    return _req_id


def call_tool(client: httpx.Client, session_id: str, tool_name: str, arguments: dict) -> dict:
    """Call a tool via the MCP protocol and return the result."""
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    resp = client.post(MCP_URL, json=payload, headers=headers)
    if resp.status_code != 200:
        print(f"  [HTTP {resp.status_code}] {resp.text[:500]}")
        return {"error": f"HTTP {resp.status_code}"}

    # MCP streamable-http may return multiline SSE or JSON
    body = resp.text.strip()
    # Parse SSE events
    result = None
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            data = line[6:]
            try:
                parsed = json.loads(data)
                if "result" in parsed or "error" in parsed:
                    result = parsed
            except json.JSONDecodeError:
                pass
        elif line.startswith("{"):
            try:
                parsed = json.loads(line)
                if "result" in parsed or "error" in parsed:
                    result = parsed
            except json.JSONDecodeError:
                pass

    return result or {"error": "No result parsed", "raw": body[:300]}


def initialize(client: httpx.Client) -> str:
    """Initialize MCP session and return session ID."""
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }
    resp = client.post(
        MCP_URL,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    session_id = resp.headers.get("Mcp-Session-Id", "")

    # Parse SSE response
    body = resp.text.strip()
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            data = line[6:]
            try:
                parsed = json.loads(data)
                if "result" in parsed:
                    print(f"  Initialized: server={parsed['result'].get('serverInfo', {}).get('name', 'unknown')}")
            except json.JSONDecodeError:
                pass

    # Send initialized notification
    notif = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    client.post(
        MCP_URL,
        json=notif,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Session-Id": session_id,
        },
    )

    return session_id


def list_tools(client: httpx.Client, session_id: str) -> list:
    """List all available tools."""
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "tools/list",
        "params": {},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Session-Id": session_id,
    }
    resp = client.post(MCP_URL, json=payload, headers=headers)
    body = resp.text.strip()
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            data = line[6:]
            try:
                parsed = json.loads(data)
                if "result" in parsed:
                    tools = parsed["result"].get("tools", [])
                    return tools
            except json.JSONDecodeError:
                pass
    return []


def print_result(name: str, result: dict) -> tuple[bool, str]:
    """Pretty print tool result and return (success, raw_text)."""
    if "error" in result and not isinstance(result.get("error"), dict):
        print(f"  ❌ {name}: {result.get('error', 'unknown error')}")
        return False, ""

    if "result" in result:
        content = result["result"].get("content", [])
        if content:
            text = content[0].get("text", "")
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "error" in parsed:
                    print(f"  ❌ {name}: {parsed['error']}")
                    return False, text
                # Truncate for display
                display = json.dumps(parsed, indent=2)
                if len(display) > 500:
                    display = display[:500] + "..."
                print(f"  ✅ {name}: {display}")
                return True, text
            except (json.JSONDecodeError, TypeError):
                display = text[:300] if len(text) > 300 else text
                print(f"  ✅ {name}: {display}")
                return True, text
        else:
            print(f"  ✅ {name}: (empty result — no resources)")
            return True, ""

    if "error" in result and isinstance(result.get("error"), dict):
        print(f"  ❌ {name}: {result['error'].get('message', 'unknown')}")
        return False, ""

    print(f"  ⚠️  {name}: {str(result)[:300]}")
    return False, ""


def main():
    passed = 0
    failed = 0
    total = 0

    def test(tool_name: str, arguments: dict) -> tuple[bool, str]:
        """Helper: call a tool, print result, track stats. Returns (success, raw_text)."""
        nonlocal passed, failed, total
        total += 1
        r = call_tool(client, session_id, tool_name, arguments)
        ok, text = print_result(tool_name, r)
        if ok:
            passed += 1
        else:
            failed += 1
        return ok, text

    with httpx.Client(timeout=60.0) as client:
        print("=" * 60)
        print("  Kubernetes MCP Server – Live Tool Tests")
        print("=" * 60)

        # Initialize
        print("\n▸ Initializing MCP session...")
        session_id = initialize(client)
        if not session_id:
            print("  ❌ Failed to initialize session")
            sys.exit(1)
        print(f"  Session: {session_id[:20]}...")

        # List tools
        print("\n▸ Listing tools...")
        tools = list_tools(client, session_id)
        tool_names = [t["name"] for t in tools]
        print(f"  Found {len(tools)} tools: {', '.join(tool_names)}")

        # ── Test: list_clusters ──────────────────────────────────────────
        print("\n▸ Testing CLUSTER MANAGEMENT tools...")
        test("list_clusters", {})
        test("cluster_health", {"cluster": CLUSTER})

        # ── Test: list_namespaces ────────────────────────────────────────
        print("\n▸ Testing NAMESPACE tools...")
        test("list_namespaces", {"cluster": CLUSTER})

        # ── Test: list_pods ──────────────────────────────────────────────
        print("\n▸ Testing POD tools...")
        ok, pods_text = test("list_pods", {"cluster": CLUSTER, "namespace": "kube-system"})

        # Extract first pod name for further tests
        pod_name = None
        if ok and pods_text:
            try:
                pods = json.loads(pods_text)
                if isinstance(pods, list) and pods:
                    pod_name = pods[0].get("name")
                elif isinstance(pods, dict) and "name" in pods:
                    pod_name = pods["name"]
            except (json.JSONDecodeError, TypeError):
                pass
            if not pod_name:
                print("  ⚠️  Could not extract pod name from list_pods response")

        if pod_name:
            test("get_pod", {"cluster": CLUSTER, "name": pod_name, "namespace": "kube-system"})
            test("get_pod_logs", {"cluster": CLUSTER, "name": pod_name, "namespace": "kube-system", "tail_lines": 10})
        else:
            print("  ⚠️  Skipping get_pod and get_pod_logs — no pod found")

        # ── Test: list_deployments ───────────────────────────────────────
        print("\n▸ Testing DEPLOYMENT tools...")
        ok, deps_text = test("list_deployments", {"cluster": CLUSTER, "namespace": "kube-system"})

        dep_name = None
        if ok and deps_text:
            try:
                deps = json.loads(deps_text)
                if isinstance(deps, list) and deps:
                    dep_name = deps[0].get("name")
            except (json.JSONDecodeError, TypeError):
                pass

        if dep_name:
            test("get_deployment", {"cluster": CLUSTER, "name": dep_name, "namespace": "kube-system"})

        # ── Test: list_nodes ─────────────────────────────────────────────
        print("\n▸ Testing NODE tools...")
        test("list_nodes", {"cluster": CLUSTER})

        # ── Test: list_events ────────────────────────────────────────────
        print("\n▸ Testing EVENT & SERVICE tools...")
        test("list_events", {"cluster": CLUSTER, "namespace": "default"})
        test("list_services", {"cluster": CLUSTER, "namespace": "default"})
        test("list_configmaps", {"cluster": CLUSTER, "namespace": "kube-system"})
        test("list_secrets", {"cluster": CLUSTER, "namespace": "default"})

        # Test get_configmap
        test("get_configmap", {"cluster": CLUSTER, "name": "coredns", "namespace": "kube-system"})

        # ── Test: RCA tools ──────────────────────────────────────────────
        print("\n▸ Testing RCA tools...")
        test("cluster_rca", {"cluster": CLUSTER})
        test("namespace_rca", {"cluster": CLUSTER, "namespace": "default"})

        if pod_name:
            test("pod_rca", {"cluster": CLUSTER, "name": pod_name, "namespace": "kube-system"})

        # ── Test: Self-heal tools ────────────────────────────────────────
        print("\n▸ Testing SELF-HEAL tools...")
        test("heal_plan", {"cluster": CLUSTER, "dry_run": True})

        # ── Test: Generic resource tools ─────────────────────────────────
        print("\n▸ Testing GENERIC RESOURCE tools...")
        test("list_resources", {"cluster": CLUSTER, "api_version": "apps/v1", "kind": "Deployment", "namespace": "kube-system"})

        # ── Summary ──────────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print(f"  Results: {passed}/{total} passed, {failed} failed")
        print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
