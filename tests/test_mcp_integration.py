"""
MCP Server integration test – verifies the server starts, accepts connections
via streamable-http transport, and responds to MCP protocol messages.

Prerequisites:
    - OpenShift CRC cluster running locally
    - Logged in via: oc login -u kubeadmin https://api.crc.testing:6443
    - Dependencies installed: pip install -e ".[dev]"

Run:
    PYTHONPATH=. pytest tests/test_mcp_integration.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import warnings

import httpx
import pytest
import urllib3

warnings.filterwarnings("ignore")
urllib3.disable_warnings()


# ── Constants ────────────────────────────────────────────────────────────────

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 18765  # Use a non-standard port to avoid conflicts
MCP_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/mcp"
TIMEOUT = 30  # seconds to wait for server to start


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _get_oc_token() -> str:
    """Get a fresh token from oc CLI."""
    return subprocess.check_output(["oc", "whoami", "-t"]).decode().strip()


@pytest.fixture(scope="module")
def mcp_server():
    """Start the MCP server in a subprocess and yield when it's ready.

    The server is killed when the test module ends.
    """
    token = _get_oc_token()

    env = os.environ.copy()
    env["CLUSTER_REGISTRY"] = json.dumps([{
        "name": "openshift-crc",
        "flavor": "openshift",
        "api_server": "https://api.crc.testing:6443",
        "openshift_oauth_token": token,
        "skip_tls_verify": True,
        "namespace": "default",
    }])
    env["MCP_SERVER_HOST"] = SERVER_HOST
    env["MCP_SERVER_PORT"] = str(SERVER_PORT)
    env["MCP_TRANSPORT"] = "streamable-http"
    env["MCP_LOG_LEVEL"] = "info"

    proc = subprocess.Popen(
        [sys.executable, "-m", "src.server"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to be ready by attempting a TCP connect or a quick POST
    start_time = time.time()
    ready = False
    while time.time() - start_time < TIMEOUT:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect((SERVER_HOST, SERVER_PORT))
            sock.close()
            ready = True
            break
        except (ConnectionRefusedError, OSError):
            pass
        time.sleep(0.5)

    # Extra wait for lifespan startup (cluster registration)
    if ready:
        time.sleep(2)

    if not ready:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        pytest.fail(
            f"MCP server failed to start within {TIMEOUT}s.\n"
            f"stdout: {stdout.decode()[-500:]}\n"
            f"stderr: {stderr.decode()[-500:]}"
        )

    yield proc

    # Cleanup: kill the server
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MCP PROTOCOL TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMCPServerStartup:
    """Test that the MCP server starts and responds to HTTP requests."""

    def test_server_is_running(self, mcp_server):
        """Verify the server is accepting connections."""
        # Verify via a simple TCP connection (process may use worker subprocess)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((SERVER_HOST, SERVER_PORT))
            sock.close()
        except (ConnectionRefusedError, OSError) as exc:
            if mcp_server.poll() is not None:
                _, stderr = mcp_server.communicate(timeout=5)
                pytest.fail(
                    f"Server process exited with code {mcp_server.returncode}.\n"
                    f"stderr: {stderr.decode()[-500:]}"
                )
            pytest.fail(f"Server not accepting connections: {exc}")

    def test_server_accepts_post(self, mcp_server):
        """Verify the server accepts POST requests on /mcp."""
        # Send a JSON-RPC initialize message
        resp = httpx.post(
            MCP_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "test-client",
                        "version": "1.0.0",
                    },
                },
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=10,
        )
        # Should get a 200 response with session info
        assert resp.status_code == 200, f"Unexpected status: {resp.status_code}, body: {resp.text[:500]}"


class TestMCPProtocol:
    """Test the MCP protocol interactions over HTTP."""

    @pytest.fixture(autouse=True)
    def setup_session(self, mcp_server):
        """Initialize an MCP session for each test."""
        self.server = mcp_server

    def _mcp_request(self, method: str, params: dict | None = None, req_id: int = 1) -> httpx.Response:
        """Send a JSON-RPC request to the MCP server."""
        body: dict = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params:
            body["params"] = params
        return httpx.post(
            MCP_URL,
            json=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=15,
        )

    def _init_session(self) -> str | None:
        """Initialize an MCP session and return the session ID."""
        resp = self._mcp_request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        })
        session_id = resp.headers.get("mcp-session-id")
        return session_id

    def test_initialize(self):
        """Test MCP initialize handshake."""
        resp = self._mcp_request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        })
        assert resp.status_code == 200, f"Init failed: {resp.text[:300]}"

    def test_list_tools(self):
        """Test listing available tools via MCP protocol."""
        session_id = self._init_session()
        headers = {"Content-Type": "application/json"}
        if session_id:
            headers["mcp-session-id"] = session_id

        # Send initialized notification first
        headers.setdefault("Accept", "application/json, text/event-stream")
        httpx.post(
            MCP_URL,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
            timeout=10,
        )

        # Now list tools
        headers["Accept"] = "application/json, text/event-stream"
        resp = httpx.post(
            MCP_URL,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
            timeout=15,
        )
        assert resp.status_code == 200, f"list_tools failed: {resp.text[:300]}"
        # Parse response - it may be SSE or JSON depending on server config
        body = resp.text
        assert "tools" in body or "result" in body, f"Unexpected response: {body[:300]}"


class TestMCPClientIntegration:
    """Test using the official MCP Python SDK client to connect to ourserver."""

    @pytest.fixture(autouse=True)
    def setup(self, mcp_server):
        self.server = mcp_server

    @pytest.mark.asyncio
    async def test_mcp_client_connect(self):
        """Connect to the MCP server with the official SDK client and list tools."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                # List tools
                tools_result = await session.list_tools()
                tool_names = [t.name for t in tools_result.tools]

                # Verify expected tools are present
                expected_tools = [
                    "list_clusters",
                    "cluster_health",
                    "list_namespaces",
                    "list_pods",
                    "get_pod",
                    "get_pod_logs",
                    "list_deployments",
                    "get_deployment",
                    "list_nodes",
                    "list_events",
                    "list_services",
                    "cluster_rca",
                    "pod_rca",
                    "namespace_rca",
                    "heal_plan",
                ]
                for expected in expected_tools:
                    assert expected in tool_names, f"Tool '{expected}' not found. Available: {tool_names}"

    @pytest.mark.asyncio
    async def test_mcp_client_call_tool(self):
        """Call a tool via the MCP protocol and verify the result."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                # Call list_clusters tool
                result = await session.call_tool("list_clusters", arguments={})
                assert result is not None
                assert not result.isError
                # The result should contain our cluster
                result_text = str(result.content)
                assert "openshift-crc" in result_text

    @pytest.mark.asyncio
    async def test_mcp_client_call_list_namespaces(self):
        """Call list_namespaces tool and verify OpenShift namespaces."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                result = await session.call_tool(
                    "list_namespaces",
                    arguments={"cluster": "openshift-crc"},
                )
                assert not result.isError
                result_text = str(result.content)
                assert "openshift-console" in result_text

    @pytest.mark.asyncio
    async def test_mcp_client_call_cluster_health(self):
        """Call cluster_health tool and verify response."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                result = await session.call_tool(
                    "cluster_health",
                    arguments={"cluster": "openshift-crc"},
                )
                assert not result.isError
                result_text = str(result.content)
                assert "reachable" in result_text

    @pytest.mark.asyncio
    async def test_mcp_client_call_list_nodes(self):
        """Call list_nodes tool and verify CRC node."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                result = await session.call_tool(
                    "list_nodes",
                    arguments={"cluster": "openshift-crc"},
                )
                assert not result.isError
                result_text = str(result.content)
                assert "crc" in result_text.lower()

    @pytest.mark.asyncio
    async def test_mcp_client_call_cluster_rca(self):
        """Call cluster_rca tool via MCP and verify the report structure."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                result = await session.call_tool(
                    "cluster_rca",
                    arguments={"cluster": "openshift-crc"},
                )
                assert not result.isError
                result_text = str(result.content)
                assert "cluster_name" in result_text or "openshift-crc" in result_text

    @pytest.mark.asyncio
    async def test_mcp_client_list_prompts(self):
        """List prompts via MCP protocol."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                prompts_result = await session.list_prompts()
                prompt_names = [p.name for p in prompts_result.prompts]

                expected_prompts = [
                    "cluster_health_check",
                    "pod_troubleshoot",
                    "self_heal_workflow",
                    "namespace_review",
                    "multi_cluster_overview",
                    "incident_response",
                ]
                for expected in expected_prompts:
                    assert expected in prompt_names, f"Prompt '{expected}' not found. Available: {prompt_names}"

    @pytest.mark.asyncio
    async def test_mcp_client_get_prompt(self):
        """Get a prompt via MCP protocol."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                prompt_result = await session.get_prompt(
                    "cluster_health_check",
                    arguments={"cluster": "openshift-crc"},
                )
                assert prompt_result is not None
                assert len(prompt_result.messages) > 0

    @pytest.mark.asyncio
    async def test_mcp_client_list_resources(self):
        """List resources via MCP protocol."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                resources_result = await session.list_resources()
                resource_uris = [str(r.uri) for r in resources_result.resources]
                assert any("clusters" in uri for uri in resource_uris), \
                    f"Expected clusters resource. Got: {resource_uris}"

    @pytest.mark.asyncio
    async def test_no_output_schema_on_tools(self):
        """Verify structured_output=False suppresses outputSchema on all tools."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    schema = getattr(tool, "outputSchema", None)
                    assert schema is None, (
                        f"Tool '{tool.name}' has outputSchema={schema}, "
                        "structured_output=False should suppress this"
                    )

    @pytest.mark.asyncio
    async def test_no_structured_content_on_call(self):
        """Verify call_tool returns TextContent without structuredContent."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool("list_clusters", arguments={})
                assert result is not None
                assert not result.isError
                assert len(result.content) >= 1, "Expected at least one TextContent block"
                sc = getattr(result, "structuredContent", None)
                assert sc is None, (
                    f"structuredContent should be None, got: {sc}"
                )
