# Workshop: AI Agent with GitHub Tools via AgentGateway

## Overview

In this workshop you will:

1. Deploy **Solo Enterprise for AgentGateway** on a Kubernetes cluster
2. Configure it to proxy MCP traffic to the **remote GitHub MCP server** (`api.githubcopilot.com`)
3. Inject GitHub authentication at the gateway layer so clients never handle tokens
4. Deploy an **AI agent** on the cluster that uses GitHub tools through agentgateway
5. Expose the agent's web UI through the same gateway

### Architecture

```
                User
                 |
                 v
           Kubernetes Cluster                        GitHub Remote
+----------------------------------------------+    MCP Server
|                                              |    +------------------+
|   Solo Agent Gateway                         |    |                  |
| +------------------------------------------+ |    | api.githubcopilot|
| |                                          | |    | .com/mcp/        |
| |  /github-agent         /mcp-github       | |    |                  |
| |  (URL rewrite)         (auth injection)  | |    | 43 tools:        |
| |       |                     |            | |    | get_me,          |
| +-------|---------------------|------------+ |    | get_file_contents|
|         |                     |              |    | create_issue,    |
| +-------v-----------------+  |               |    | create_pr, ...   |
| |  My Agent               |  |  HTTPS        |    |                  |
| |  Claude LLM             |  +---------------+--->|                  |
| |  MCP Client             |  (injects PAT    |    |                  |
| |  FastAPI + Web UI       |   + TLS + SNI)   |    |                  |
| +-------------------------+                  |    |                  |
|                                              |    +------------------+
+----------------------------------------------+
```

**Request flow:**
```
                                  +------------------+
                                  |  Anthropic API   |
                                  +--------+---------+
                                           ^
                                           | Claude LLM calls
                                           |
Browser --> AgentGateway --> Agent Pod --> AgentGateway --> GitHub MCP
            /github-agent    (Claude)     /mcp-github      (remote)
            (URL rewrite)                 (injects PAT)

1. Browser sends chat message to /github-agent/chat
2. AgentGateway rewrites path and forwards to Agent Pod
3. Agent sends user message + tool definitions to Claude
4. Claude decides to call a GitHub tool (e.g. get_me)
5. Agent calls /mcp-github on AgentGateway via MCP
6. AgentGateway injects the PAT and forwards to api.githubcopilot.com
7. Tool result flows back: GitHub --> AgentGateway --> Agent
8. Agent sends tool result to Claude, gets final answer
9. Response flows back: Agent --> AgentGateway --> Browser
```

### Why AgentGateway in the middle?

Without agentgateway, every agent and MCP client needs the GitHub PAT configured locally. With agentgateway:

- **Centralized auth** — The PAT lives in the cluster (HTTPRoute), not in every agent's config
- **Observability** — Every tool call flows through the gateway and is visible in metrics/traces
- **Policy enforcement** — Add rate limiting, JWT auth, tool-level access control without touching the MCP server
- **Unified ingress** — Both the agent UI and MCP proxy share the same gateway endpoint
- **Protocol handling** — AgentGateway manages MCP session routing and TLS termination to the upstream

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| Kubernetes cluster | GKE, EKS, or kind |
| `kubectl` | Cluster access |
| `helm` | Install agentgateway |
| GitHub PAT | Personal access token with `repo`, `read:org`, `read:user` scopes |
| AgentGateway license key | [Contact Solo.io](https://www.solo.io/company/contact) |
| `npx` (optional) | For MCP Inspector verification |

---

## Step 1: Install AgentGateway

### 1.1 Deploy the Gateway API CRDs

```bash
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.4.0/standard-install.yaml
```

### 1.2 Install AgentGateway CRDs

```bash
export AGENTGATEWAY_LICENSE_KEY=<your-license-key>

helm upgrade -i enterprise-agentgateway-crds \
  oci://us-docker.pkg.dev/solo-public/enterprise-agentgateway/charts/enterprise-agentgateway-crds \
  --create-namespace \
  --namespace enterprise-agentgateway \
  --version 2.1.1
```

### 1.3 Install AgentGateway control plane

```bash
helm upgrade -i enterprise-agentgateway \
  oci://us-docker.pkg.dev/solo-public/enterprise-agentgateway/charts/enterprise-agentgateway \
  -n enterprise-agentgateway \
  --version 2.1.1 \
  --set-string licensing.licenseKey=${AGENTGATEWAY_LICENSE_KEY}
```

### 1.4 Verify the control plane is running

```bash
kubectl get pods -n enterprise-agentgateway
```

Expected output:
```
NAME                                       READY   STATUS    RESTARTS   AGE
enterprise-agentgateway-xxxxxxxxxx-xxxxx   1/1     Running   0          30s
```

### 1.5 Create a Gateway

```bash
kubectl apply -f- <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: agentgateway
  namespace: enterprise-agentgateway
spec:
  gatewayClassName: enterprise-agentgateway
  listeners:
  - allowedRoutes:
      namespaces:
        from: All
    name: http
    port: 8080
    protocol: HTTP
EOF
```

Wait for the gateway to be programmed and get its address:

```bash
kubectl get gateway agentgateway -n enterprise-agentgateway
```

Expected output:
```
NAME           CLASS                     ADDRESS        PROGRAMMED   AGE
agentgateway   enterprise-agentgateway   <EXTERNAL-IP>  True         30s
```

Save the gateway address:
```bash
export GATEWAY_ADDRESS=$(kubectl get gateway agentgateway -n enterprise-agentgateway -o jsonpath='{.status.addresses[0].value}')
echo "Gateway address: $GATEWAY_ADDRESS"
```

---

## Step 2: Configure the GitHub MCP backend

The remote GitHub MCP server is hosted at `api.githubcopilot.com` and requires HTTPS connections with a Bearer token for authentication.

### 2.1 Create the AgentgatewayBackend

This resource tells agentgateway where the upstream MCP server lives:

```bash
kubectl apply -f- <<EOF
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayBackend
metadata:
  name: github-mcp-backend
  namespace: enterprise-agentgateway
spec:
  mcp:
    targets:
    - name: mcp-target
      static:
        host: api.githubcopilot.com
        port: 443
        path: /mcp/
        policies:
          tls:
            sni: api.githubcopilot.com
EOF
```

Key fields:
- **`host: api.githubcopilot.com`** — The remote GitHub MCP server
- **`port: 443`** — HTTPS
- **`path: /mcp/`** — The MCP endpoint path on the remote server
- **`tls.sni`** — Server Name Indication for TLS

Verify it's accepted:
```bash
kubectl get agentgatewaybackend github-mcp-backend -n enterprise-agentgateway
```

Expected output:
```
NAME                 ACCEPTED   AGE
github-mcp-backend   True       5s
```

### 2.2 Create the HTTPRoute

This routes MCP traffic arriving at `/mcp-github` to the GitHub backend, injecting the GitHub PAT as an Authorization header:

```bash
export GH_PAT=<your-github-personal-access-token>

kubectl apply -f- <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: mcp-github
  namespace: enterprise-agentgateway
spec:
  parentRefs:
  - name: agentgateway
    namespace: enterprise-agentgateway
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /mcp-github
      filters:
        - type: RequestHeaderModifier
          requestHeaderModifier:
            set:
              - name: Authorization
                value: "Bearer ${GH_PAT}"
      backendRefs:
      - name: github-mcp-backend
        group: agentgateway.dev
        kind: AgentgatewayBackend
EOF
```

Key details:
- **Path `/mcp-github`** — The path clients use to reach the GitHub MCP server through agentgateway
- **`RequestHeaderModifier`** — Injects the `Authorization: Bearer <PAT>` header on every request. The MCP client never needs to know the token.
- **`AgentgatewayBackend` reference** — Routes to the backend we created above

Verify:
```bash
kubectl get httproute mcp-github -n enterprise-agentgateway
```

Expected output:
```
NAME         HOSTNAMES   AGE
mcp-github               5s
```

---

## Step 3: Verify the connection

### 3.1 Quick test with curl

Run a full MCP handshake to confirm everything works:

```bash
# Initialize an MCP session
curl -s -X POST http://${GATEWAY_ADDRESS}:8080/mcp-github \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "workshop-test", "version": "1.0"}
    }
  }'
```

You should see a response containing:
```json
{"jsonrpc":"2.0","id":1,"result":{"capabilities":{"tools":{}},"protocolVersion":"2025-03-26","serverInfo":{"name":"github-mcp-server"}}}
```

### 3.2 Test with MCP Inspector

For a visual experience, use the MCP Inspector tool:

1. Port-forward the agentgateway proxy (skip this step if using the LoadBalancer address directly):
   ```bash
   kubectl port-forward deployment/agentgateway -n enterprise-agentgateway 8080:8080
   ```

2. Launch the MCP Inspector:
   ```bash
   npx @modelcontextprotocol/inspector
   ```

3. In the MCP Inspector UI:
   - **Transport Type**: `Streamable HTTP`
   - **URL**: `http://<GATEWAY_ADDRESS>:8080/mcp-github` (or `http://localhost:8080/mcp-github` if port-forwarding)
   - Click **Connect**

4. Go to the **Tools** tab and click **List Tools**. You should see 20+ GitHub tools:
   - `get_me` — Get your GitHub profile
   - `get_file_contents` — Read files from a repository
   - `search_repositories` — Search GitHub repos
   - `create_issue` — Create a new issue
   - `create_pull_request` — Create a PR
   - `create_branch` — Create a new branch
   - And many more...

5. Select the `get_me` tool and click **Run Tool**. You should see your GitHub profile information.

---

## Step 4: Deploy the AI agent

Now deploy an AI agent on the cluster that connects to the GitHub MCP server through agentgateway. The agent uses Claude as the LLM and exposes a web chat UI.

The agent image (`rvennam/github-agent`) is a Python app that:
- Connects to agentgateway's in-cluster MCP endpoint on startup
- Discovers all 43 GitHub tools via `tools/list`
- On each user message, sends the conversation + tool definitions to Claude
- Executes any tool calls Claude requests via MCP through agentgateway
- Returns the final response

### 4.1 Create the namespace and secrets

```bash
kubectl create namespace github-agent

export ANTHROPIC_API_KEY=<your-anthropic-api-key>

kubectl create secret generic agent-secrets \
  --from-literal=ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}" \
  -n github-agent
```

### 4.2 Deploy the agent

```bash
kubectl apply -f- <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: github-agent
  namespace: github-agent
spec:
  replicas: 1
  selector:
    matchLabels:
      app: github-agent
  template:
    metadata:
      labels:
        app: github-agent
    spec:
      containers:
      - name: agent
        image: rvennam/github-agent:latest
        env:
        - name: ANTHROPIC_API_KEY
          valueFrom:
            secretKeyRef:
              name: agent-secrets
              key: ANTHROPIC_API_KEY
        - name: MCP_URL
          value: "http://agentgateway.enterprise-agentgateway.svc.cluster.local:8080/mcp-github"
        ports:
        - containerPort: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: github-agent
  namespace: github-agent
spec:
  selector:
    app: github-agent
  ports:
  - port: 8000
    targetPort: 8000
EOF
```

Key details:
- **`MCP_URL`** — Points to agentgateway's **in-cluster** service address at the `/mcp-github` path. The agent never talks directly to GitHub — it always goes through agentgateway.
- **`ANTHROPIC_API_KEY`** — The LLM API key, stored as a Kubernetes secret.

### 4.3 Verify the agent is running

```bash
kubectl get pods -n github-agent
```

Expected output:
```
NAME                            READY   STATUS    RESTARTS   AGE
github-agent-xxxxxxxxxx-xxxxx   1/1     Running   0          30s
```

Check the logs to confirm MCP connection:
```bash
kubectl logs -n github-agent -l app=github-agent
```

Expected output:
```
Connecting to MCP server at http://agentgateway.enterprise-agentgateway.svc.cluster.local:8080/mcp-github...
Connected! 43 tools available.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

---

## Step 5: Expose the agent through AgentGateway

Rather than port-forwarding, expose the agent's web UI through the same agentgateway proxy. This means the gateway serves both the agent UI and proxies MCP traffic.

```bash
kubectl apply -f- <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: github-agent
  namespace: github-agent
spec:
  parentRefs:
  - name: agentgateway
    namespace: enterprise-agentgateway
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /github-agent
      filters:
        - type: URLRewrite
          urlRewrite:
            path:
              type: ReplacePrefixMatch
              replacePrefixMatch: /
      backendRefs:
      - name: github-agent
        kind: Service
        port: 8000
EOF
```

Key details:
- **`/github-agent` path** — The agent UI is accessible at this path on the gateway
- **`URLRewrite`** — Strips the `/github-agent` prefix before forwarding to the agent pod, which serves its UI at `/`

---

## Step 6: Use the agent

Open the agent in your browser:

```
http://<GATEWAY_ADDRESS>:8080/github-agent
```

Try these prompts:
- *"Who am I on GitHub?"*
- *"What are my most recent repositories?"*
- *"Show me the README from rvennam/istio-workshop"*
- *"Search for repos related to agentgateway"*
- *"Create an issue in my repo titled 'Bug: fix login flow'"*

Each prompt triggers the following chain:
1. Browser sends the message to the agent pod (via agentgateway at `/github-agent`)
2. Agent sends the message + tool definitions to Claude
3. Claude decides which GitHub tools to call
4. Agent executes tool calls via MCP through agentgateway (at `/mcp-github`)
5. AgentGateway injects the PAT and forwards to `api.githubcopilot.com`
6. Tool results flow back through the same chain
7. Claude formulates a response using the tool results

---

## What just happened?

Let's recap all the resources and how they fit together:

| Resource | Kind | Namespace | Purpose |
|----------|------|-----------|---------|
| `agentgateway` | Gateway | enterprise-agentgateway | Envoy-based proxy with LoadBalancer IP |
| `github-mcp-backend` | AgentgatewayBackend | enterprise-agentgateway | Points to `api.githubcopilot.com:443` with TLS |
| `mcp-github` | HTTPRoute | enterprise-agentgateway | Routes `/mcp-github` to GitHub, injects PAT |
| `agent-secrets` | Secret | github-agent | Anthropic API key for the agent |
| `github-agent` | Deployment + Service | github-agent | AI agent pod with Claude + MCP client |
| `github-agent` | HTTPRoute | github-agent | Exposes agent UI at `/github-agent` via the gateway |

---

## Bonus: Connect external MCP clients

The MCP proxy at `/mcp-github` isn't only for the in-cluster agent. Any MCP client can use it.

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "github-via-agentgateway": {
      "type": "streamableHttp",
      "url": "http://<GATEWAY_ADDRESS>:8080/mcp-github"
    }
  }
}
```

### Cursor / VS Code

In MCP settings, add:
- **Transport**: Streamable HTTP
- **URL**: `http://<GATEWAY_ADDRESS>:8080/mcp-github`

No authentication needed on the client side — agentgateway handles it.

---

## Cleanup

```bash
kubectl delete namespace github-agent
kubectl delete httproute mcp-github -n enterprise-agentgateway
kubectl delete agentgatewaybackend github-mcp-backend -n enterprise-agentgateway
```

To fully uninstall agentgateway:
```bash
kubectl delete gateway agentgateway -n enterprise-agentgateway
helm uninstall enterprise-agentgateway -n enterprise-agentgateway
helm uninstall enterprise-agentgateway-crds -n enterprise-agentgateway
kubectl delete namespace enterprise-agentgateway
```
