# Why ReadyAgents?

MCP connectivity is not durable orchestration.

This page records two public reports about gaps between MCP wiring and durable
workflow execution, plus one design note about how modern MCP asks a question. It
states plainly what ReadyAgents 0.9.0 does and does not do. Each verdict is about
our own software only.

## LangGraph MCP surface: MCP-exposed runs remain stateless

Sources: [langchain-ai/langgraph issue 8725](https://github.com/langchain-ai/langgraph/issues/8725) and [LangSmith server MCP docs](https://docs.langchain.com/langsmith/server-mcp)

Quote issue 8725:

> When a LangGraph Platform deployment is exposed as an MCP server, every tool call is executed as a temporary run: no durable state exists even while it runs (`checkpointer=None`), and its auto-created thread is deleted on completion.

Quote LangSmith docs:

> The current LangGraph MCP implementation does not support sessions. Each `/mcp` request is stateless and independent.

ReadyAgents 0.9.0: PARTIAL. We persist local workflow state and support resume/approval pauses. An explicitly started, loopback-only Streamable HTTP command now returns a durable `run_id` that can be polled, approved, or cancelled without holding the original request open. That is a local authenticated task handle, not a hosted recovery surface: stopping the command stops the listener and in-process executor. A record may remain on disk; nothing resumes it automatically. This comparison is about the MCP surface, not LangGraph's other surfaces.

## n8n MCP Server Trigger: a long synchronous call can lose the result, and HITL is unsupported

Sources: [n8n community thread on MCP Server Trigger 502 after ~125s](https://community.n8n.io/t/mcp-server-trigger-returns-502-after-125s-while-long-running-subworkflow-completes-successfully-and-is-triggered-twice/309413) and [n8n MCP Server tools reference](https://docs.n8n.io/connect/connect-to-n8n-mcp-server/mcp-server-tools-reference/)

Quote the 2026-08-25 thread:

> MCP request starts → long-running subworkflow continues → MCP request/connection times out after about 120 seconds → external caller receives 502 → n8n subworkflow continues and eventually succeeds

Quote the reply:

> A tool that legitimately runs 2 to 4 minutes cannot answer synchronously.

Quote the current MCP tools reference:

> Executing workflows with multi-step forms or any kind of human-in-the-loop interactions isn't supported.

ReadyAgents 0.9.0: PARTIAL. We checkpoint locally and have an approval node. A disconnected caller can now recover a local result by polling `GET /runs/{run_id}` after `POST /runs` returns 202, and can decide or cooperatively cancel on the same authenticated loopback door. That does not make ReadyAgents a hosted MCP Server Trigger: the door is an explicit foreground command, and process death loses the in-flight executor. n8n's instance-level `execute_workflow` already returns an execution ID immediately; this page is only about the MCP Server Trigger surface.

## Design note: modern MCP uses retry-shaped multi-round trips

Sources: [MCP specification: Multi Round-Trip Requests (MRTR)](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr) and [Python SDK elicitation handlers](https://py.sdk.modelcontextprotocol.io/handlers/elicitation/)

Quote the spec:

> Multi Round-Trip Requests (MRTR) was introduced in this version of the MCP specification. This replaces the previous approach of sending server-initiated requests.

Quote the Python SDK:

> A resolver works on every connection. For a client on a legacy connection the SDK sends it the question directly; on a 2026-07-28 connection the SDK returns the question from the call, and the client's next attempt carries the answer.

This is a design note, not a competitor gap. The ask is now a stateless retry-shaped resolver; ReadyAgents sidesteps it with a persisted approval node. 0.9.0 still does not implement MRTR or protocol elicitation. Approval pause remains out-of-band persisted HITL. An Unreleased localhost approval page can list those pauses; it is not MCP elicitation.
