from __future__ import annotations

import hashlib
import logging
import shutil
import sys
import time
from pathlib import Path

from llama_stack_client import LlamaStackClient

from rummager_agent.config import Settings
from rummager_agent.git_repo import (
    GitSource,
    STACK_ONLY_MARKER,
    clone_repository,
    resolve_git_source,
    workspace_git_summary_for_prompt,
)
from rummager_agent.k8s_pods import (
    PodLogSnapshot,
    collect_pod_logs,
    excerpt_for_fingerprint,
    list_pods_matching_label,
    load_kube_config,
    log_has_error_indicators,
)
from rummager_agent.llama_tools import run_tool_assisted_fix
from rummager_agent.state_store import StateStore

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert software engineer running inside **Rummager**, a Kubernetes log-remediation agent.

You are given application **pod logs** that contain errors and the identity of the target repository
(owner/repo and branch). This deployment may have **no local git clone** (the pod may only reach Llama
Stack, not git hosts); in that case you must use **GitHub MCP for all repository access**—read, search,
apply changes, and open pull requests. If a local workspace exists with a clone, you may use
`workspace_list_files`, `workspace_read_file`, and `workspace_write_file` there as a convenience, but
GitHub MCP remains the source of truth for publishing.

Goals:
1. Perform **root cause analysis** from the logs. Use the log excerpts as primary evidence.
2. Inspect and change code using **GitHub MCP** (required when there is no local clone). Use workspace
   tools only when the user message indicates a populated clone. For **every** GitHub MCP call that accepts
   a branch, tag, or SHA (file contents, tree, search, etc.), use the **branch/ref named in the user
   message** (`RUMMAGER_GIT_BRANCH`)—that is the application revision to analyze; do not silently use
   the repository default branch.
3. Apply **minimal, correct fixes**; publish with **GitHub MCP only** (commit/branch/push/PR as your tools allow).
   The PR **base** must be that same branch/ref—**do not** assume `main` unless it was explicitly given.
4. This Python process never calls GitHub's HTTP API; only MCP tools do.

If this process also has a **Kubernetes MCP** tool group, use it only when extra cluster context helps.

Constraints:
- Prefer small, reviewable changes; do not refactor unrelated code.
- Do not commit secrets or credentials.
- After you believe the fix is complete, summarize root cause and changes in plain language (include the
  PR link from MCP if you opened one).
"""


def _register_mcp_endpoints(client: LlamaStackClient, settings: Settings) -> None:
    for reg in settings.parsed_mcp_registrations():
        try:
            client.toolgroups.register(
                toolgroup_id=reg.toolgroup_id,
                provider_id=reg.provider_id,
                mcp_endpoint={"uri": reg.mcp_uri},
            )
            logger.info("Registered MCP toolgroup %s", reg.toolgroup_id)
        except Exception as e:
            logger.warning(
                "Could not register MCP toolgroup %s (may already exist): %s",
                reg.toolgroup_id,
                e,
            )


def _resolve_model_id(client: LlamaStackClient, configured: str | None) -> str:
    if configured:
        return configured
    models = client.models.list()
    if not models:
        raise RuntimeError("LLAMA_STACK_MODEL_ID is unset and Llama Stack returned no models")
    mid = models[0].id
    logger.info("Using first available Llama Stack model: %s", mid)
    return mid


def _incident_fingerprint(namespace: str, pod_uid: str, excerpt: str) -> str:
    raw = f"{namespace}\n{pod_uid}\n{excerpt}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _build_user_prompt(
    snapshot: PodLogSnapshot,
    git_summary: str,
    repo_path: Path,
    branch_hint: str,
    base_branch: str,
    owner: str,
    repo: str,
    stack_only: bool,
    label_name: str,
    label_value: str,
    dry_run_no_pr: bool,
) -> str:
    logs_blob = snapshot.combined_log if snapshot.combined_log.strip() else "(no log lines collected)"
    pr_block = (
        "\n\n**Dry run:** Do **not** open a pull request or push branches; only analyze and describe the fix.\n"
        if dry_run_no_pr
        else ""
    )
    mode = (
        "### Repository mode (stack-only)\n"
        "There is **no** `git clone` in this pod. Use **only GitHub MCP** to read and modify "
        f"`{owner}/{repo}` on branch/ref **`{base_branch}`**. The workspace path below is scratch only.\n\n"
        if stack_only
        else "### Repository mode (local clone)\n"
        "A shallow clone exists under the workspace path; you may use workspace tools and/or GitHub MCP.\n\n"
    )
    return f"""## Kubernetes workload

Namespace: `{snapshot.namespace}`
Pod: `{snapshot.pod_name}` (UID `{snapshot.pod_uid}`, phase `{snapshot.phase}`)
Selector: `{label_name}={label_value}`

### Collected pod logs
{logs_blob}

## Target GitHub repository
**`{owner}/{repo}`**

**Configured ref (`RUMMAGER_GIT_BRANCH`):** **`{base_branch}`** — use this ref for **all** GitHub MCP reads
(file/tree/search APIs that take a ref) and as the **PR merge base** (target branch).

{mode}## Workspace path on disk
`{repo_path}` (may be empty except a marker file when stack-only)

Recent history / instructions:
```
{git_summary}
```

When your changes are ready, use **GitHub MCP** to publish: suggested head branch name `{branch_hint}`;
PR **base** **must** be **`{base_branch}`**—not `main` unless `{base_branch}` is literally `main`.{pr_block}

Then write a short summary of the root cause and the fix (include the PR link from MCP if you opened one).
"""


def process_log_incident(
    settings: Settings,
    state: StateStore,
    client: LlamaStackClient,
    model_id: str,
    src: GitSource,
    snapshot: PodLogSnapshot,
    incident_id: str,
) -> None:
    ws = Path(settings.workspace_root) / incident_id
    if ws.exists():
        shutil.rmtree(ws)

    stack_only = not bool(src.clone_url.strip())
    if stack_only:
        ws.mkdir(parents=True, exist_ok=True)
        (ws / STACK_ONLY_MARKER).write_text(
            "Stack-only: use GitHub MCP for repository access.\n",
            encoding="utf-8",
        )
        logger.info(
            "No RUMMAGER_GIT_CLONE_URL — workspace is scratch only; model must use GitHub MCP for %s/%s",
            src.owner,
            src.repo,
        )
    else:
        try:
            clone_repository(src, ws, settings.git_clone_depth)
        except Exception as e:
            logger.exception(
                "Clone failed for %s/%s: %s. For stack-only deployments unset RUMMAGER_GIT_CLONE_URL "
                "and set RUMMAGER_GIT_REPOSITORY.",
                src.owner,
                src.repo,
                e,
            )
            state.mark_incident_processed(
                incident_id,
                {
                    "reason": "clone_failed",
                    "pod": snapshot.pod_name,
                    "namespace": snapshot.namespace,
                },
            )
            return

    summary = workspace_git_summary_for_prompt(ws, src)
    branch_hint = f"{settings.pr_branch_prefix}/pod-{snapshot.pod_name}"[:250]
    user_prompt = _build_user_prompt(
        snapshot,
        summary,
        ws,
        branch_hint,
        settings.git_branch,
        src.owner,
        src.repo,
        stack_only,
        settings.pod_label_name,
        settings.pod_label_value,
        settings.dry_run_no_pr,
    )

    logger.info(
        "Invoking Llama Stack (model=%s) for pod %s/%s (incident %s…)",
        model_id,
        snapshot.namespace,
        snapshot.pod_name,
        incident_id[:12],
    )
    try:
        llm_summary = run_tool_assisted_fix(
            client=client,
            model_id=model_id,
            tool_group_ids=settings.tool_group_id_list,
            repo_root=ws,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_iterations=settings.max_llm_iterations,
        )
    except Exception:
        logger.exception("Llama Stack run failed for %s/%s", snapshot.namespace, snapshot.pod_name)
        state.mark_incident_processed(
            incident_id,
            {
                "reason": "llm_failed",
                "pod": snapshot.pod_name,
                "namespace": snapshot.namespace,
            },
        )
        return

    logger.info("Model finished with summary (excerpt): %s", llm_summary[:2000])

    if settings.dry_run_no_pr:
        logger.info("RUMMAGER_DRY_RUN_NO_PR set; model was instructed not to open a PR")
        pr_via = "dry_run"
    else:
        logger.info("Expecting GitHub MCP (only) for any push/PR; see model summary for links")
        pr_via = "github_mcp"

    state.mark_incident_processed(
        incident_id,
        {
            "pod": snapshot.pod_name,
            "namespace": snapshot.namespace,
            "repository": f"{src.owner}/{src.repo}",
            "pr_via": pr_via,
            "model_summary_excerpt": llm_summary[:8000],
        },
    )


def run_forever(settings: Settings, state: StateStore) -> None:
    load_kube_config()
    try:
        src = resolve_git_source(
            settings.git_repository,
            settings.git_clone_url,
            settings.git_branch,
        )
    except ValueError as e:
        raise RuntimeError(str(e)) from e

    client = LlamaStackClient(
        base_url=settings.llama_stack_base_url,
        api_key=settings.llama_stack_api_key,
        timeout=600.0,
    )
    _register_mcp_endpoints(client, settings)
    model_id = _resolve_model_id(client, settings.llama_stack_model_id)

    while True:
        try:
            pods = list_pods_matching_label(
                settings.kubernetes_namespace,
                settings.pod_label_name,
                settings.pod_label_value,
            )
            if pods:
                logger.debug("Found %s pod(s) matching label selector", len(pods))

            for pod in pods:
                snapshot = collect_pod_logs(
                    settings.kubernetes_namespace,
                    pod,
                    tail_lines=settings.log_tail_lines,
                    max_bytes_per_container=settings.per_container_log_budget,
                    since_seconds=settings.log_max_age_seconds,
                )
                text = snapshot.combined_log
                if not log_has_error_indicators(
                    text,
                    settings.error_substring_list,
                    settings.compiled_error_regex,
                ):
                    continue

                excerpt = excerpt_for_fingerprint(text)
                incident_id = _incident_fingerprint(snapshot.namespace, snapshot.pod_uid, excerpt)
                if state.is_incident_processed(incident_id):
                    continue

                logger.info(
                    "Error-like log content in %s/%s; starting remediation (incident %s…)",
                    snapshot.namespace,
                    snapshot.pod_name,
                    incident_id[:12],
                )
                process_log_incident(
                    settings,
                    state,
                    client,
                    model_id,
                    src,
                    snapshot,
                    incident_id,
                )
        except Exception:
            logger.exception("Poll iteration failed")

        time.sleep(settings.poll_interval_seconds)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    settings = Settings()
    state = StateStore(settings.state_file_path)
    run_forever(settings, state)


if __name__ == "__main__":
    main()
