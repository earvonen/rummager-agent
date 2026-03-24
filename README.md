# Rummager (rummager-agent)

**Rummager** is a Python service for **Kubernetes / OpenShift** that **polls pod logs** for workloads selected by a **label**, detects **error-like** lines, and drives a **Llama Stack** model with **MCP tools** (GitHub, optional Kubernetes) plus local **workspace** file tools. The model performs root cause analysis, may fetch extra source via **GitHub MCP**, applies fixes in a **clone** of your repo (branch from configuration), and opens a **pull request** (GitHub MCP and/or in-process REST when `GITHUB_TOKEN` is set).

## What it does

1. On a configurable interval, lists **Pods** in a namespace whose labels match **`RUMMAGER_POD_LABEL_NAME`=`RUMMAGER_POD_LABEL_VALUE`**.
2. Reads **recent logs** for every container (and init container) in each pod. Optionally restricts to the last **`RUMMAGER_LOG_MAX_AGE_SECONDS`** seconds (Kubernetes `sinceSeconds`); always capped by `tail_lines` and byte limits per container.
3. If logs match **substring** rules (`RUMMAGER_ERROR_LOG_SUBSTRINGS`, case-insensitive) and/or an optional **regex** (`RUMMAGER_ERROR_LOG_REGEX`), treats that as an incident.
4. **Deduplicates** incidents with a fingerprint (namespace + pod UID + tail excerpt) stored in **`RUMMAGER_STATE_FILE`** under `processed_incidents`.
5. **Clones** `RUMMAGER_GIT_CLONE_URL` at `RUMMAGER_GIT_BRANCH` into a workspace (public HTTPS if no token; use **`GITHUB_TOKEN`** for private repos and optional REST PR creation).
6. Calls **Llama Stack** (`chat.completions` + `tool_runtime`) with MCP tools + local `workspace_*` tools.
7. Optionally uses the **GitHub REST API** from this process to commit, push, and open a PR **only if** `GITHUB_TOKEN` is set; otherwise PR creation is left to the **GitHub MCP** (and clone must be reachable without a token if the repo is public).

## Requirements

- **Kubernetes** access (in-cluster or kubeconfig): `list` pods and `get` **pods/log** in the target namespace.
- A reachable **Llama Stack** HTTP endpoint and a **model** id (or rely on the first model returned by the stack).
- **MCP tool groups** registered with that stack (at minimum **GitHub** for browsing source and opening PRs; **Kubernetes** optional), with IDs that match `RUMMAGER_TOOL_GROUP_IDS`.
- **RBAC** for the pod’s `ServiceAccount`: `get`, `list`, `watch` on `pods` and `pods/log` (see `deploy/openshift.yaml`).

## Install and run (local)

```bash
cd rummager-agent
pip install .
export RUMMAGER_KUBERNETES_NAMESPACE=my-namespace
export RUMMAGER_POD_LABEL_NAME=app
export RUMMAGER_POD_LABEL_VALUE=my-service
export RUMMAGER_GIT_CLONE_URL=https://github.com/org/application.git
export RUMMAGER_GIT_BRANCH=main
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

The image installs `git` (required for clone and local repo operations).

### Build on OpenShift with Tekton

The `deploy/tekton/` tasks and pipeline build this image; adjust `git-url` / `git-revision` in your `PipelineRun` to point at this repository.

## OpenShift deployment

Example manifests: [`deploy/openshift.yaml`](deploy/openshift.yaml). Adjust image reference, namespace, and ConfigMap values.

- **`RUMMAGER_KUBERNETES_NAMESPACE`** (alias **`RUMMAGER_WATCH_NAMESPACE`**) — namespace where labeled pods run.
- **`LLAMA_STACK_BASE_URL`** — Llama Stack service URL.
- **`RUMMAGER_TOOL_GROUP_IDS`** — exact tool group IDs your stack registers (include GitHub).
- **`RUMMAGER_GIT_CLONE_URL`** / **`RUMMAGER_GIT_BRANCH`** — application repo and branch (checkout + PR base).

GitHub credentials are **not** required in the Deployment if the **GitHub MCP** on Llama Stack already has a PAT.

## Configuration (environment variables)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RUMMAGER_KUBERNETES_NAMESPACE` | **Yes** | — | Namespace to list pods and read logs. Alias: `RUMMAGER_WATCH_NAMESPACE`. |
| `RUMMAGER_POD_LABEL_NAME` | **Yes** | — | Pod label **key** (e.g. `app`). |
| `RUMMAGER_POD_LABEL_VALUE` | **Yes** | — | Pod label **value**. |
| `RUMMAGER_GIT_CLONE_URL` | **Yes** | — | Git clone URL (`https://…` or `git@github.com:…`). Used to clone and to parse `owner/repo` for REST PRs. |
| `RUMMAGER_GIT_BRANCH` | **Yes** | — | Branch to check out and as GitHub PR **base**. |
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
| `GITHUB_TOKEN` | No | — | If set: clone private repos via HTTPS and/or open PR via GitHub REST from this pod. |
| `RUMMAGER_GIT_CLONE_DEPTH` | No | `50` | Shallow clone depth. |
| `RUMMAGER_MAX_LLM_ITERATIONS` | No | `40` | Max chat completion rounds (tool loops). |
| `RUMMAGER_PR_BRANCH_PREFIX` | No | `rummager-agent` | Suggested branch prefix for the model / REST PR path. |
| `RUMMAGER_DRY_RUN_NO_PR` | No | `false` | If `true`, skip PR creation after the model run. |

### Registering MCP servers at startup

If your stack does not persist tool group registration:

```json
[{"toolgroup_id":"mcp::github","provider_id":"model-context-protocol","mcp_uri":"http://github-mcp:8080/sse"}]
```

Pass as **`RUMMAGER_MCP_REGISTRATIONS_JSON`** (single-line string in a `ConfigMap`).

## GitHub: MCP vs `GITHUB_TOKEN`

- **Default:** No PAT in this app for the API. **GitHub MCP** (on Llama Stack) carries the PAT for operations the model performs. **Clone** uses plain HTTPS (public repos) unless **`GITHUB_TOKEN`** is set.
- **With `GITHUB_TOKEN`:** Authenticated **clone** (private repos) and optional in-process **commit / push / PR** via the GitHub REST API (`git_repo.py`).

## State file

`RUMMAGER_STATE_FILE` stores JSON like:

```json
{
  "processed_incidents": {
    "<sha256-hex>": {
      "pod": "name",
      "namespace": "ns",
      "repository": "org/repo",
      "pull_request": "https://github.com/.../pull/1",
      "pr_via": "github_mcp | github_rest | dry_run"
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
| `src/rummager_agent/git_repo.py` | Clone URL → `GitSource`, clone, optional REST PR |
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
