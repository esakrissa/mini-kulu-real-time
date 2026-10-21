import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import mcp.types
from livekit.agents import function_tool

import agent_tools as at


class FakeServer:
    """Stands in for MCPServerHTTP: connects after `connect_s`, offers one tool."""

    def __init__(self, tool_name="search", connect_s=0.0, call_s=0.0, fail_connects=0):
        self.tool_name = tool_name
        self.connect_s = connect_s
        self.call_s = call_s
        self.fail_connects = fail_connects
        self.initialized = False
        self.connects = 0
        self.calls: list[dict] = []
        self.closed = False

    async def initialize(self):
        self.connects += 1
        await asyncio.sleep(self.connect_s)
        if self.fail_connects:
            self.fail_connects -= 1
            raise ConnectionError("refused")
        self.initialized = True

    async def list_tools(self):
        async def call(raw_arguments):
            self.calls.append(raw_arguments)
            await asyncio.sleep(self.call_s)
            return f"result for {raw_arguments}"

        return [function_tool(call, raw_schema={"name": self.tool_name, "parameters": {}})]

    async def aclose(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.fillers = 0

    @asynccontextmanager
    async def with_filler(self, source, *, delay=0):
        self.fillers += 1
        yield


def remote(server, timeout_s=1.0, name="search"):
    return at.RemoteTool(server, name, timeout_s)


async def test_call_returns_the_tool_text():
    tool = remote(FakeServer())
    assert await tool.call({"q": 1}) == "result for {'q': 1}"


async def test_slow_server_times_out_and_reports_latency():
    seen = []
    tool = remote(FakeServer(call_s=1.0), timeout_s=0.05)
    outcome = await at.timed(tool, {}, lambda name, s: seen.append((name, s)))
    assert outcome.text is None and outcome.error == "no answer within 0.05 s"
    assert seen and seen[0][0] == "search" and 0.04 < seen[0][1] < 0.5


async def test_deadline_includes_connecting_but_keeps_the_connection_going():
    server = FakeServer(connect_s=0.1)
    tool = remote(server, timeout_s=0.02)
    assert (await at.timed(tool, {}, None)).text is None
    # The timed-out call didn't cancel the connect, so the next call finds it ready.
    await asyncio.sleep(0.15)
    assert server.initialized
    assert (await at.timed(tool, {}, None)).text is not None
    assert server.connects == 1


async def test_failed_connect_is_retried_on_the_next_call():
    server = FakeServer(fail_connects=1)
    tool = remote(server)
    assert (await at.timed(tool, {}, None)).error == "ConnectionError: refused"
    assert (await at.timed(tool, {}, None)).text == "result for {}"
    assert server.connects == 2


async def test_dropped_connection_reconnects():
    server = FakeServer()
    tool = remote(server)
    await tool.call({})
    server.initialized = False  # what MCPServer does when its client task ends
    await tool.call({})
    assert server.connects == 2


async def test_missing_tool_fails_softly():
    tool = remote(FakeServer(tool_name="other"))
    outcome = await at.timed(tool, {}, None)
    assert outcome.text is None and outcome.error.startswith("LookupError")


async def test_web_search_fixes_its_arguments_and_caps_calls():
    server = FakeServer()
    search = at.web_search_tool(remote(server), max_calls=2)
    ctx = FakeContext()
    for _ in range(3):
        last = await search(ctx, "weather in Ubud")
    assert len(server.calls) == 2
    assert server.calls[0] == {
        "query": "weather in Ubud",
        "objective": "weather in Ubud",
        "numResults": at.SEARCH_RESULTS,
    }
    assert "used all 2 web searches" in last
    assert ctx.fillers == 2


async def test_web_search_timeout_becomes_a_sentence():
    search = at.web_search_tool(remote(FakeServer(call_s=1.0), timeout_s=0.02), max_calls=3)
    assert "didn't come back in time" in await search(FakeContext(), "news")


async def test_project_file_is_pinned_to_the_repository():
    server = FakeServer(tool_name=at.GITHUB_TOOL)
    read = at.project_file_tool(remote(server, name=at.GITHUB_TOOL), repo="me/project")
    await read(FakeContext(), "agent/rejoin.py")
    assert server.calls == [{"owner": "me", "repo": "project", "path": "agent/rejoin.py"}]
    assert "README.md" in read.info.description


async def test_project_file_rejects_paths_outside_the_repository():
    server = FakeServer(tool_name=at.GITHUB_TOOL)
    read = at.project_file_tool(remote(server, name=at.GITHUB_TOOL))
    for path in ["/etc/passwd", "../other-repo/README.md", "docs/../../x", ""]:
        assert "Not a file path" in await read(FakeContext(), path)
    assert server.calls == []


async def test_web_search_reports_each_call_to_the_page():
    events = []
    search = at.web_search_tool(remote(FakeServer()), max_calls=1, on_event=events.append)
    await search(FakeContext(), "weather in Ubud")
    await search(FakeContext(), "and tomorrow?")
    running, ok, limit_running, limit = events
    assert running["status"] == "running" and running["args"] == {"query": "weather in Ubud"}
    assert ok["id"] == running["id"] and ok["status"] == "ok"
    assert ok["result"].startswith("result for") and ok["seconds"] >= 0
    assert limit["id"] == limit_running["id"] != ok["id"]
    assert limit["status"] == "error" and "1 searches per call" in limit["error"]


async def test_failed_call_reports_the_error_to_the_page():
    events = []
    tool = remote(FakeServer(tool_name=at.GITHUB_TOOL, call_s=1.0), 0.02, at.GITHUB_TOOL)
    read = at.project_file_tool(tool, on_event=events.append)
    await read(FakeContext(), "README.md")
    assert [e["status"] for e in events] == ["running", "error"]
    assert events[1]["error"] == "no answer within 0.02 s"
    assert events[1]["mcp_tool"] == at.GITHUB_TOOL


async def test_a_broken_event_sink_never_breaks_the_call():
    def sink(_event):
        raise RuntimeError("room gone")

    search = at.web_search_tool(remote(FakeServer()), max_calls=1, on_event=sink)
    assert (await search(FakeContext(), "news")).startswith("result for")


def test_clip():
    assert at.clip("abc", 5) == "abc"
    assert at.clip("abcdef", 3) == "abc\n[cut off here]"


def test_text_of_reads_text_and_embedded_files():
    result = mcp.types.CallToolResult(
        content=[
            mcp.types.TextContent(type="text", text="downloaded"),
            mcp.types.EmbeddedResource(
                type="resource",
                resource=mcp.types.TextResourceContents(uri="repo://x/a.py", text="print(1)"),
            ),
        ]
    )
    ctx = SimpleNamespace(result=result)
    assert at.text_of(ctx) == "downloaded\nprint(1)"
