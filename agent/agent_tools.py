"""Agent tools from remote MCP servers: web search (Exa) and this project's repository (GitHub).

Both servers speak streamable HTTP and are reached with the SDK's MCPServerHTTP. They aren't
handed to the agent as MCPToolsets, for three reasons:

- A toolset connects before the session starts, so a slow or broken server would delay the
  greeting (GitHub's takes ~1.6 s to connect from Bali). Here each server connects in the
  background when the call starts, and a tool call waits for it within its own deadline.
- The model sees one small tool per server with the arguments it shouldn't choose already fixed
  (three search results; this repository). GitHub's `get_file_contents` takes any owner/repo,
  so pinning the repository has to happen in our code, not in the token.
- Every call has a deadline and a size cap, and a failure turns into one sentence the model can
  answer from ("the search took too long"), never an exception or a long silence.

Every call is also reported to the page as it happens (ToolTrace): what was asked, how long it
took, and what came back or what went wrong, so a caller can see why an answer sounds the way
it does.

A tool is only built when its credential is set (EXA_API_KEY, GH_MCP_TOKEN), so the agent
runs as before without them.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from livekit.agents import RunContext, function_tool, llm
from livekit.agents.llm import mcp

logger = logging.getLogger("mini-kulu-agent")

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
EXA_TOOL = "web_search_exa"
# The read-only "repos" toolset of GitHub's remote MCP server.
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/x/repos/readonly"
GITHUB_TOOL = "get_file_contents"
PROJECT_REPO = "esakrissa/mini-kulu-real-time"

SEARCH_RESULTS = 3
# Caps on what goes back into the prompt: the LLM reads it all before its first token.
MAX_SEARCH_CHARS = 3_000
MAX_FILE_CHARS = 12_000
# Spoken only if the tool is still running after this long with nothing else being said. Kept
# out of the chat history: the model took it as its own line and then waited for the user.
FILLER = "One moment, let me check."
FILLER_DELAY_S = 0.5

# The files worth opening, so the model doesn't have to guess paths.
PROJECT_FILES = {
    "README.md": "overview, demo, measured latency, resilience, roadmap",
    "docs/architecture.md": "system diagram, one voice turn, failure handling, deploy flow",
    "docs/latency-results.md": "latency per stage on GCP, the e2-micro CPU finding",
    "docs/migration-plan.md": "AWS to GCP migration plan",
    "agent/agent.py": "the voice pipeline (STT, LLM, TTS) and the session",
    "agent/llm_watchdog.py": "LLM timeout, backup model, spoken apology",
    "agent/tts_fallback.py": "backup voice",
    "agent/rejoin.py": "rejoining after a browser refresh",
    "agent/turn_metrics.py": "per-stage latency metrics",
    "agent/agent_tools.py": "these tools (web search, reading this repository)",
    "infra/agent.tf": "the agent VM on GCP, in OpenTofu",
    "infra/main.tf": "network, service account, secrets",
    ".github/workflows/infra.yml": "tofu plan on pull requests, apply on main",
}

# What the page gets of a result: enough to see what the model read.
MAX_EVENT_CHARS = 4_000

LatencySink = Callable[[str, float], None]
# Receives one dict per tool event (see ToolTrace); the agent sends them to the page.
EventSink = Callable[[dict[str, Any]], None]


def filler(context: RunContext) -> Callable[[int], Any]:
    return lambda _step: context.session.say(FILLER, add_to_chat_ctx=False)


def text_of(ctx: mcp.MCPToolResultContext) -> str:
    """The text in an MCP tool result: text parts and embedded text resources (a file)."""
    parts = []
    for item in ctx.result.content:
        if getattr(item, "type", None) == "text":
            parts.append(item.text)
        elif getattr(item, "type", None) == "resource" and hasattr(item.resource, "text"):
            parts.append(item.resource.text)
    return "\n".join(parts)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[cut off here]"


class RemoteTool:
    """One tool of a remote MCP server, connected in the background and called with a deadline."""

    def __init__(
        self, server: mcp.MCPServer, tool_name: str, timeout_s: float, label: str = ""
    ) -> None:
        self.server = server
        self.tool_name = tool_name
        self.timeout_s = timeout_s
        self.label = label  # the server's name on the page
        self._connecting: asyncio.Task[Any] | None = None

    def connect(self) -> None:
        """Start connecting, or reconnect if the last attempt failed or the connection dropped
        since. Returns at once."""
        task = self._connecting
        dead = (
            task is not None
            and task.done()
            and (task.cancelled() or task.exception() is not None or not self.server.initialized)
        )
        if task is None or dead:
            self._connecting = asyncio.create_task(
                self._connect(), name=f"mcp-connect-{self.tool_name}"
            )

    async def _connect(self) -> Any:
        if not self.server.initialized:
            await self.server.initialize()
        for tool in await self.server.list_tools():
            if tool.info.name == self.tool_name:
                return tool
        raise LookupError(f"{self.server!r} has no tool {self.tool_name!r}")

    async def call(self, arguments: dict[str, Any]) -> str:
        """The tool's text. Raises TimeoutError past the deadline (connecting included)."""
        self.connect()
        assert self._connecting is not None
        async with asyncio.timeout(self.timeout_s):
            # Shielded: a call that times out mustn't cancel the connection the next call needs.
            tool = await asyncio.shield(self._connecting)
            return await tool(arguments)

    async def aclose(self) -> None:
        if self._connecting is not None and not self._connecting.done():
            self._connecting.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await self.server.aclose()


@dataclass
class Outcome:
    text: str | None
    error: str | None = None


async def timed(
    tool: RemoteTool, arguments: dict[str, Any], on_latency: LatencySink | None
) -> Outcome:
    """Call the tool and report its latency. The text, or why there is none."""
    start = time.perf_counter()
    try:
        text = await tool.call(arguments)
        return Outcome(text) if text else Outcome(None, "the server returned nothing")
    except TimeoutError:
        logger.warning("%s timed out after %.1f s", tool.tool_name, tool.timeout_s)
        return Outcome(None, f"no answer within {tool.timeout_s:g} s")
    except Exception as e:
        logger.warning("%s failed: %s", tool.tool_name, e)
        return Outcome(None, f"{type(e).__name__}: {e}".rstrip(": "))
    finally:
        if on_latency is not None:
            on_latency(tool.tool_name, time.perf_counter() - start)


class ToolTrace:
    """One tool call as the page sees it: a "running" event now, then "ok" or "error".

    Events share an id, so the page updates one card. A broken sink never breaks the call.
    """

    def __init__(
        self, sink: EventSink | None, name: str, remote: RemoteTool, args: dict[str, Any]
    ) -> None:
        self._sink = sink
        self._start = time.perf_counter()
        self._base = {
            "id": uuid.uuid4().hex[:12],
            "tool": name,
            "server": remote.label,
            "mcp_tool": remote.tool_name,
            "args": args,
        }
        self._emit("running")

    def ok(self, result: str) -> None:
        self._emit("ok", result=clip(result, MAX_EVENT_CHARS))

    def fail(self, error: str) -> None:
        self._emit("error", error=error)

    def _emit(self, status: str, **fields: Any) -> None:
        if self._sink is None:
            return
        event = {**self._base, "status": status, **fields}
        if status != "running":
            event["seconds"] = round(time.perf_counter() - self._start, 3)
        try:
            self._sink(event)
        except Exception:
            logger.exception("failed to report a tool event")


def exa_search(api_key: str, timeout_s: float) -> RemoteTool:
    server = mcp.MCPServerHTTP(
        EXA_MCP_URL,
        headers={"x-api-key": api_key},
        transport_type="streamable_http",
        allowed_tools=[EXA_TOOL],
        client_session_timeout_seconds=timeout_s,
        tool_result_resolver=text_of,
    )
    return RemoteTool(server, EXA_TOOL, timeout_s, label="Exa MCP")


def github_files(token: str, timeout_s: float) -> RemoteTool:
    server = mcp.MCPServerHTTP(
        GITHUB_MCP_URL,
        headers={"Authorization": f"Bearer {token}"},
        # The SDK guesses SSE for a URL that doesn't end in /mcp; this one is streamable HTTP.
        transport_type="streamable_http",
        allowed_tools=[GITHUB_TOOL],
        client_session_timeout_seconds=timeout_s,
        tool_result_resolver=text_of,
    )
    return RemoteTool(server, GITHUB_TOOL, timeout_s, label="GitHub MCP")


def web_search_tool(
    search: RemoteTool,
    max_calls: int,
    on_latency: LatencySink | None = None,
    on_event: EventSink | None = None,
) -> llm.FunctionTool:
    """A `web_search` tool for one call, with at most `max_calls` searches (they're billed)."""
    calls = 0

    @function_tool
    async def web_search(context: RunContext, query: str) -> str:
        """Search the web for facts you can't know yourself: news, weather, prices, opening
        hours, anything recent or local.

        Args:
            query: What to find, as a full sentence.
        """
        nonlocal calls
        trace = ToolTrace(on_event, "web_search", search, {"query": query})
        if calls >= max_calls:
            logger.info("web_search limit reached (%d per call)", max_calls)
            trace.fail(f"limit reached: {max_calls} searches per call")
            # Said plainly, or the model tells the user that search "isn't available".
            return (
                f"You've used all {max_calls} web searches allowed in one call. Tell the user "
                "that, then answer from what you know."
            )
        calls += 1
        async with context.with_filler(filler(context), delay=FILLER_DELAY_S):
            outcome = await timed(
                search,
                {"query": query, "objective": query, "numResults": SEARCH_RESULTS},
                on_latency,
            )
        if not outcome.text:
            trace.fail(outcome.error or "no result")
            return (
                "The search didn't come back in time. Say you couldn't check just now, "
                "then answer from what you know if you can."
            )
        text = clip(outcome.text, MAX_SEARCH_CHARS)
        trace.ok(text)
        return text

    return web_search


def valid_path(path: str) -> bool:
    parts = path.split("/")
    return 0 < len(path) <= 200 and not path.startswith("/") and ".." not in parts


def project_file_tool(
    files: RemoteTool,
    repo: str = PROJECT_REPO,
    on_latency: LatencySink | None = None,
    on_event: EventSink | None = None,
) -> llm.FunctionTool:
    """A `read_project_file` tool that can only read `repo` (owner/name)."""
    owner, name = repo.split("/", 1)
    file_list = "; ".join(f"{p}: {what}" for p, what in PROJECT_FILES.items())

    async def read_project_file(context: RunContext, path: str) -> str:
        trace = ToolTrace(on_event, "read_project_file", files, {"repo": repo, "path": path})
        if not valid_path(path):
            trace.fail("not a path in this repository")
            return f"Not a file path in this repository: {path!r}."
        async with context.with_filler(filler(context), delay=FILLER_DELAY_S):
            outcome = await timed(files, {"owner": owner, "repo": name, "path": path}, on_latency)
        if not outcome.text:
            trace.fail(outcome.error or "no result")
            return f"Couldn't read {path} just now. Answer from what you know, and say so."
        text = clip(outcome.text, MAX_FILE_CHARS)
        trace.ok(text)
        return text

    return function_tool(
        read_project_file,
        description=(
            "Read a file of your own source code and docs (the mini-kulu project) to explain "
            "how you're built. Open one file, the most relevant one. Files: " + file_list
        ),
    )
