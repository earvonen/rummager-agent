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

SYSTEM_PROMPT = """You are an expert software engineer running inside **Rummager**, a Kubernetes log-monitoring agent.

You are given **pod logs** from a workload selected by a **label selector** (a set of pods, not a single fixed pod),
plus the identity of the target repository (owner/repo and branch/ref).

Use **GitHub MCP for all repository access** (read and search). For every GitHub MCP call that accepts a branch, tag,
or SHA, use the configured ref from the user message (`RUMMAGER_GIT_BRANCH`)—do not silently use the repository default
branch.

Mission:
1. Perform **root cause analysis** from the provided logs. Quote the exact log lines that demonstrate the failure.
2. Using GitHub MCP, locate the most likely responsible code path on `RUMMAGER_GIT_BRANCH` and cite it precisely
   (file paths + function/class names; include links if your tools provide them).
3. Create a **GitHub issue** (via GitHub MCP) describing:
   - What happened (symptoms + impact)
   - Evidence (key log excerpts)
   - Root cause hypothesis, tied to specific code locations
   - A concrete, minimal fix plan (what to change), without changing public API paths

Non-negotiable constraints:
- **Do not modify code**. Do not create commits, branches, or pull requests. This agent files issues only.
- **API stability**: Do not propose changes that rename/move client-visible HTTP paths/URLs (routes, ingress paths,
  OpenAPI paths, webhooks). Fix behavior behind existing endpoints; if a spec mismatch is proven, call it out explicitly
  in the issue instead of shipping a breaking path change.
- This Python process never calls GitHub's HTTP API; only MCP tools do.
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
    extra_fix_constraints: str | None,
) -> str:
    logs_blob = snapshot.combined_log if snapshot.combined_log.strip() else "(no log lines collected)"
    pr_block = (
        "\n\n**Dry run:** Do **not** create or modify GitHub issues; only analyze and describe the root cause.\n"
        if dry_run_no_pr
        else ""
    )
    extra_block = ""
    if extra_fix_constraints and extra_fix_constraints.strip():
        extra_block = (
            "\n## Additional operator constraints\n\n"
            f"{extra_fix_constraints.strip()}\n"
        )
    mode = (
        "### Repository mode (stack-only)\n"
        "There is **no** `git clone` in this pod. Use **only GitHub MCP** to read/search the repo on the configured ref. "
        "The workspace path below is scratch only.\n\n"
        if stack_only
        else "### Repository mode (local clone)\n"
        "A shallow clone exists under the workspace path. Do not modify code in this workspace; use it only for context.\n\n"
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
(file/tree/search APIs that take a ref). Do not assume `main` unless `{base_branch}` is literally `main`.

{mode}## Workspace path on disk
`{repo_path}` (may be empty except a marker file when stack-only)

Recent history / instructions:
```
{git_summary}
```

### Mandatory: do not break callers
Do not recommend changes that rename/move client-visible HTTP/API **paths** (routes, ingress paths, OpenAPI
paths, webhooks). Recommend fixes **behind** existing endpoints only. If a spec mismatch is proven, call it
out explicitly in the issue rather than proposing a breaking path change.{extra_block}

When you are ready, use **GitHub MCP** to create a **GitHub issue** in `{owner}/{repo}` describing the root
cause and a minimal fix plan (no code changes in this run).{pr_block}

Then write a short summary of the root cause and the issue you created (include the issue link if available).
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
    issue_title_hint = f"{settings.pr_branch_prefix}: error in pod {snapshot.pod_name}"[:250]
    user_prompt = _build_user_prompt(
        snapshot,
        summary,
        ws,
        issue_title_hint,
        settings.git_branch,
        src.owner,
        src.repo,
        stack_only,
        settings.pod_label_name,
        settings.pod_label_value,
        settings.dry_run_no_pr,
        settings.fix_constraints,
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
            settings=settings,
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
        logger.info("RUMMAGER_DRY_RUN_NO_PR set; model was instructed not to create an issue")
        pr_via = "dry_run"
    else:
        logger.info("Expecting GitHub MCP (only) to create an issue; see model summary for links")
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
