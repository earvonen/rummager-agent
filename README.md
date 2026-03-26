# Rummager (rummager-agent)

**Rummager** is a Python service for **Kubernetes / OpenShift** that **polls pod logs** for workloads selected by a **label**, detects **error-like** lines, and drives a **Llama Stack** model with **MCP tools** (GitHub, optional Kubernetes) plus optional local **workspace** tools. The model performs root cause analysis and is instructed to **use GitHub MCP** for repository access and PRs. **Production pods** can omit `RUMMAGER_GIT_CLONE_URL` so the container **only needs egress to Llama Stack** (no `git` traffic to GitHub); set **`RUMMAGER_GIT_REPOSITORY`** (`owner/repo`) and **`RUMMAGER_GIT_BRANCH`** so the model knows what to target. **This process does not call the GitHub HTTP API** from Python.

## What it does

1. On a configurable interval, lists **Pods** in a namespace whose labels match **`RUMMAGER_POD_LABEL_NAME`=`RUMMAGER_POD_LABEL_VALUE`**.
2. Reads **recent logs** for every container (and init container) in each pod. Optionally restricts to the last **`RUMMAGER_LOG_MAX_AGE_SECONDS`** seconds (Kubernetes `sinceSeconds`); always capped by `tail_lines` and byte limits per container.
3. If logs match **substring** rules (`RUMMAGER_ERROR_LOG_SUBSTRINGS`, case-insensitive) and/or an optional **regex** (`RUMMAGER_ERROR_LOG_REGEX`), treats that as an incident.
4. **Deduplicates** incidents with a fingerprint (namespace + pod UID + tail excerpt) stored in **`RUMMAGER_STATE_FILE`** under `processed_incidents`.
5. Optionally **clones** `RUMMAGER_GIT_CLONE_URL` when set (dev / networks that allow git hosts). When **unset**, creates a **scratch workspace** only—**GitHub MCP** (via Llama Stack) is the sole way to read or change the repo.
6. Calls **Llama Stack** (`chat.completions` + `tool_runtime`) with MCP tools + local `workspace_*` tools (workspace may be empty except a marker file in stack-only mode).
7. The model uses **GitHub MCP** for GitHub operations (including opening a PR). Rummager records state and a **summary excerpt**; PR links come from the model text.

## Requirements

- **Kubernetes** access (in-cluster or kubeconfig): `list` pods and `get` **pods/log** in the target namespace.
- A reachable **Llama Stack** HTTP endpoint and a **model** id (or rely on the first model returned by the stack).
- **MCP tool groups** registered with that stack (at minimum **GitHub** so the model can publish PRs; **Kubernetes** optional), with IDs that match `RUMMAGER_TOOL_GROUP_IDS`.
- **RBAC** for the pod’s `ServiceAccount`: `get`, `list`, `watch` on `pods` and `pods/log` (see `deploy/openshift.yaml`).
- **GitHub MCP** must be configured with credentials your stack expects (e.g. PAT on the MCP server)—not in the Rummager Deployment.

## Install and run (local)

```bash
cd rummager-agent
pip install .
export RUMMAGER_KUBERNETES_NAMESPACE=my-namespace
export RUMMAGER_POD_LABEL_NAME=app
export RUMMAGER_POD_LABEL_VALUE=my-service
export RUMMAGER_GIT_REPOSITORY=org/application
export RUMMAGER_GIT_BRANCH=main
# Optional local clone (omit in stack-only / no-git-egress pods):
export RUMMAGER_GIT_CLONE_URL=https://github.com/org/application.git
export LLAMA_STACK_BASE_URL=http://llamastack-service:8321
export RUMMAGER_TOOL_GROUP_IDS=mcp-openshift,mcp-github
# Optional: .env file is also loaded (pydantic-settings)
rummager-agent
# or: python -m rummager_agent
```

Use a valid kubeconfig (or run inside the cluster with in-cluster config).

## Container image

Build with the included **Containerfile**:

```bash
podman build -f Containerfile -t rummager-agent:latest .
```

The image installs `git` (used when `RUMMAGER_GIT_CLONE_URL` is set).

### Build on OpenShift with Tekton

The `deploy/tekton/` tasks and pipeline build this image; adjust `git-url` / `git-revision` in your `PipelineRun` to point at this repository.

## OpenShift deployment

Example manifests: [`deploy/openshift.yaml`](deploy/openshift.yaml). Adjust image reference, namespace, and ConfigMap values.

- **`RUMMAGER_KUBERNETES_NAMESPACE`** (alias **`RUMMAGER_WATCH_NAMESPACE`**) — namespace where labeled pods run.
- **`LLAMA_STACK_BASE_URL`** — Llama Stack service URL.
- **`RUMMAGER_TOOL_GROUP_IDS`** — exact tool group IDs your stack registers (include GitHub MCP).
- **`RUMMAGER_GIT_REPOSITORY`** (`owner/repo`) — target repo for prompts and state (required when clone URL is omitted).
- **`RUMMAGER_GIT_BRANCH`** — PR base / ref for the model.
- **`RUMMAGER_GIT_CLONE_URL`** — optional; omit if the pod may only reach Llama Stack.

Do **not** put GitHub PATs in the Rummager pod for GitHub.com API calls; configure **GitHub MCP** on Llama Stack instead.

## Configuration (environment variables)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RUMMAGER_KUBERNETES_NAMESPACE` | **Yes** | — | Namespace to list pods and read logs. Alias: `RUMMAGER_WATCH_NAMESPACE`. |
| `RUMMAGER_POD_LABEL_NAME` | **Yes** | — | Pod label **key** (e.g. `app`). |
| `RUMMAGER_POD_LABEL_VALUE` | **Yes** | — | Pod label **value**. |
| `RUMMAGER_GIT_REPOSITORY` | No* | — | `owner/repo` on GitHub. **Required** if `RUMMAGER_GIT_CLONE_URL` is unset; optional otherwise (parsed from clone URL if omitted). |
| `RUMMAGER_GIT_CLONE_URL` | No* | — | Git clone URL. **Omit** when the pod has no egress to git hosts (stack-only). |
| `RUMMAGER_GIT_BRANCH` | **Yes** | — | Branch/ref for optional local checkout and PR **base** for the model. |
| `LLAMA_STACK_BASE_URL` | **Yes** | — | Base URL of Llama Stack. |
| `RUMMAGER_TOOL_GROUP_IDS` | **Yes** | — | Comma-separated tool group IDs (e.g. `mcp::kubernetes,mcp::github`). |
| `RUMMAGER_POLL_INTERVAL_SECONDS` | No | `120` | Sleep between poll loops. |
| `RUMMAGER_STATE_FILE` | No | `/tmp/rummager-agent-state.json` | JSON file of processed incident fingerprints. |
| `RUMMAGER_WORKSPACE_ROOT` | No | `/tmp/rummager-workspaces` | Parent directory for per-incident clone directories. |
| `RUMMAGER_LOG_TAIL_LINES` | No | `3000` | `tail_lines` per container when reading logs. |
| `RUMMAGER_LOG_MAX_AGE_SECONDS` | No | — | If set, only include log lines from the last *N* seconds (API `sinceSeconds`). Unset = no time cutoff (still bounded by tail/bytes). |
| `RUMMAGER_LOG_TRUNCATE_BYTES` | No | `65536` | Max bytes retained per container log chunk (tail). |
| `RUMMAGER_LOG_PER_CONTAINER_MAX_BYTES` | No | — | Overrides per-container byte cap if set. |
| `RUMMAGER_ERROR_LOG_SUBSTRINGS` | No | `ERROR,FATAL,Exception,Traceback,panic` | Comma-separated substrings; **any** match (case-insensitive) flags an error. |
| `RUMMAGER_ERROR_LOG_REGEX` | No | — | Optional regex; if it matches the log blob, the pod is flagged (even if substrings miss). |
| `LLAMA_STACK_MODEL_ID` | No | — | Model id; if unset, the **first** model from `GET /models` is used. |
| `LLAMA_STACK_API_KEY` | No | — | Bearer token for Llama Stack (if required). |
| `RUMMAGER_MCP_REGISTRATIONS_JSON` | No | — | JSON array to register MCP SSE endpoints at startup. |
| `RUMMAGER_GIT_CLONE_DEPTH` | No | `50` | Shallow clone depth. |
| `RUMMAGER_MAX_LLM_ITERATIONS` | No | `40` | Max chat completion rounds (tool loops). |
| `RUMMAGER_PR_BRANCH_PREFIX` | No | `rummager-agent` | Suggested head-branch prefix for the model (GitHub MCP). |
| `RUMMAGER_DRY_RUN_NO_PR` | No | `false` | If `true`, the model is instructed **not** to open a PR or push. |

\*At least one of **`RUMMAGER_GIT_REPOSITORY`** or **`RUMMAGER_GIT_CLONE_URL`** must be set. For **stack-only** deployments, set **`RUMMAGER_GIT_REPOSITORY`** and leave **`RUMMAGER_GIT_CLONE_URL`** unset.

### Registering MCP servers at startup

If your stack does not persist tool group registration:

```json
[{"toolgroup_id":"mcp::github","provider_id":"model-context-protocol","mcp_uri":"http://github-mcp:8080/sse"}]
```

Pass as **`RUMMAGER_MCP_REGISTRATIONS_JSON`** (single-line string in a `ConfigMap`).

## GitHub access and network egress

- **Rummager → Llama Stack** only is enough for **stack-only** mode: omit **`RUMMAGER_GIT_CLONE_URL`**, set **`RUMMAGER_GIT_REPOSITORY`**, and rely on **GitHub MCP** (reached by Llama Stack, not by this pod) for all repo I/O and PRs.
- If **`RUMMAGER_GIT_CLONE_URL`** is set, this pod runs **`git clone`** to that URL (still no GitHub REST API in Python).
- **Kubernetes API** traffic for pod logs is in-cluster, not general internet.

## State file

`RUMMAGER_STATE_FILE` stores JSON like:

```json
{
  "processed_incidents": {
    "<sha256-hex>": {
      "pod": "name",
      "namespace": "ns",
      "repository": "org/repo",
      "pr_via": "github_mcp | dry_run",
      "model_summary_excerpt": "…"
    }
  }
}
```

Delete an entry or the file to re-process a fingerprint. Mount a **PersistentVolumeClaim** on `/var/lib/rummager-agent` if you need state across pod restarts.

## Project layout

| Path | Purpose |
|------|---------|
| `src/rummager_agent/main.py` | Poll loop, orchestration |
| `src/rummager_agent/k8s_pods.py` | List pods by label, read logs |
| `src/rummager_agent/git_repo.py` | `GitSource` resolution, optional local `git clone` |
| `src/rummager_agent/llama_tools.py` | Llama Stack tool loop (MCP + workspace tools) |
| `src/rummager_agent/config.py` | Settings / env parsing |
| `src/rummager_agent/state_store.py` | Processed incident persistence |
| `deploy/openshift.yaml` | Example SA, Role, ConfigMap, Deployment |

## Limitations

- **Duplicate MCP tool names** across tool groups are not supported (the second definition is skipped).
- Large logs are **truncated** to keep prompts bounded.
- Incident identity is derived from the **tail** of the log; a **new** error appended later changes the excerpt and may create a **new** incident (and potentially another PR).

## License

See repository root for license terms if present.
