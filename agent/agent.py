import os
import json
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MCP_URL = os.environ.get("MCP_URL", "http://agentgateway.enterprise-agentgateway.svc.cluster.local:8080/mcp-github")
MODEL = os.environ.get("MODEL", "claude-sonnet-4-20250514")

app = FastAPI()

# --- MCP Client ---

class MCPClient:
    def __init__(self, url: str):
        self.url = url
        self.session_id = None
        self.tools = []
        self.client = httpx.Client(timeout=30)

    def initialize(self):
        resp = self.client.post(
            self.url,
            json={
                "jsonrpc": "2.0", "method": "initialize", "id": 1,
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "github-agent", "version": "1.0"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
        self.session_id = resp.headers.get("mcp-session-id")
        # send initialized notification
        self.client.post(
            self.url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self._headers(),
        )

    def _headers(self):
        h = {"Accept": "application/json, text/event-stream"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def list_tools(self):
        resp = self.client.post(
            self.url,
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
            headers=self._headers(),
        )
        data = self._parse_sse(resp.text)
        self.tools = data.get("result", {}).get("tools", [])
        return self.tools

    def call_tool(self, name: str, arguments: dict):
        resp = self.client.post(
            self.url,
            json={
                "jsonrpc": "2.0", "method": "tools/call", "id": 3,
                "params": {"name": name, "arguments": arguments},
            },
            headers=self._headers(),
        )
        data = self._parse_sse(resp.text)
        return data.get("result", {})

    def _parse_sse(self, text: str) -> dict:
        for line in text.strip().split("\n"):
            if line.startswith("data: "):
                return json.loads(line[6:])
        try:
            return json.loads(text)
        except Exception:
            return {}


# --- Claude Agent ---

def tools_to_anthropic_format(mcp_tools):
    """Convert MCP tool definitions to Anthropic tool_use format."""
    result = []
    for t in mcp_tools:
        result.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
        })
    return result


def run_agent(user_message: str, mcp: MCPClient, conversation: list):
    anthropic_tools = tools_to_anthropic_format(mcp.tools)
    conversation.append({"role": "user", "content": user_message})

    system = (
        "You are a helpful GitHub assistant. You have access to GitHub tools via MCP. "
        "Use them to help the user interact with GitHub repositories, issues, pull requests, and more. "
        "Be concise in your responses."
    )

    # agentic loop
    for _ in range(10):
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 4096,
                "system": system,
                "tools": anthropic_tools,
                "messages": conversation,
            },
            timeout=60,
        )
        msg = resp.json()

        if msg.get("error"):
            return f"Error: {msg['error']}"

        # add assistant message to conversation
        conversation.append({"role": "assistant", "content": msg["content"]})

        if msg["stop_reason"] == "end_turn":
            # extract text
            texts = [b["text"] for b in msg["content"] if b["type"] == "text"]
            return "\n".join(texts)

        if msg["stop_reason"] == "tool_use":
            tool_results = []
            for block in msg["content"]:
                if block["type"] == "tool_use":
                    tool_name = block["name"]
                    tool_input = block["input"]
                    result = mcp.call_tool(tool_name, tool_input)
                    content_parts = result.get("content", [])
                    text = "\n".join(p.get("text", "") for p in content_parts)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": text[:10000],
                    })
            conversation.append({"role": "user", "content": tool_results})

    return "Reached maximum iterations."


# --- Web UI ---

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<title>GitHub Agent</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; height: 100vh; display: flex; flex-direction: column; }
  .header { background: #161b22; border-bottom: 1px solid #30363d; padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
  .header svg { width: 32px; height: 32px; fill: #c9d1d9; }
  .header h1 { font-size: 18px; font-weight: 600; }
  .header .badge { background: #238636; color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 12px; }
  .chat { flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  .msg { max-width: 80%; padding: 12px 16px; border-radius: 12px; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; font-size: 14px; }
  .msg.user { align-self: flex-end; background: #1f6feb; color: #fff; }
  .msg.assistant { align-self: flex-start; background: #161b22; border: 1px solid #30363d; }
  .msg.system { align-self: center; color: #8b949e; font-size: 12px; font-style: italic; }
  .input-area { background: #161b22; border-top: 1px solid #30363d; padding: 16px 24px; display: flex; gap: 12px; }
  .input-area input { flex: 1; background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 12px 16px; color: #c9d1d9; font-size: 14px; outline: none; }
  .input-area input:focus { border-color: #1f6feb; }
  .input-area button { background: #238636; color: #fff; border: none; border-radius: 8px; padding: 12px 24px; font-size: 14px; cursor: pointer; font-weight: 600; }
  .input-area button:hover { background: #2ea043; }
  .input-area button:disabled { opacity: 0.5; cursor: not-allowed; }
  .typing { display: none; align-self: flex-start; color: #8b949e; font-size: 13px; padding: 8px 16px; }
  .typing.show { display: block; }
</style>
</head>
<body>
<div class="header">
  <svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
  <h1>GitHub Agent</h1>
  <span class="badge">via AgentGateway</span>
</div>
<div class="chat" id="chat">
  <div class="msg system">Connected to GitHub MCP server through AgentGateway. Ask me anything about your GitHub repos!</div>
</div>
<div class="typing" id="typing">Agent is thinking...</div>
<div class="input-area">
  <input type="text" id="input" placeholder="Ask about your GitHub repos..." autofocus />
  <button id="send" onclick="sendMessage()">Send</button>
</div>
<script>
const chat = document.getElementById('chat');
const input = document.getElementById('input');
const typing = document.getElementById('typing');
const sendBtn = document.getElementById('send');
input.addEventListener('keydown', e => { if (e.key === 'Enter' && !sendBtn.disabled) sendMessage(); });
async function sendMessage() {
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  addMsg(text, 'user');
  sendBtn.disabled = true;
  typing.classList.add('show');
  try {
    const base = window.location.pathname.replace(/\/$/, '');
    const res = await fetch(base + '/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text}),
    });
    const data = await res.json();
    addMsg(data.response, 'assistant');
  } catch (e) {
    addMsg('Error: ' + e.message, 'system');
  }
  typing.classList.remove('show');
  sendBtn.disabled = false;
  input.focus();
}
function addMsg(text, role) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}
</script>
</body>
</html>"""

# --- State ---

mcp_client = None
conversation = []

@app.on_event("startup")
async def startup():
    global mcp_client
    mcp_client = MCPClient(MCP_URL)
    print(f"Connecting to MCP server at {MCP_URL}...")
    mcp_client.initialize()
    mcp_client.list_tools()
    print(f"Connected! {len(mcp_client.tools)} tools available.")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/health")
async def health():
    return {"status": "ok", "tools": len(mcp_client.tools) if mcp_client else 0}


@app.post("/chat")
async def chat_endpoint(request: Request):
    body = await request.json()
    user_msg = body.get("message", "")
    response = run_agent(user_msg, mcp_client, conversation)
    return JSONResponse({"response": response})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
