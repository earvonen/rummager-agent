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
    clone_repository,
    create_branch_commit_push_pr,
    git_repo_summary,
    git_source_from_clone_url,
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

You are given application **pod logs** that contain errors, and a **local Git clone** of the application
repository (branch from configuration). Your goals:

1. Perform **root cause analysis** from the logs. Use the log excerpts as primary evidence.
2. When you need additional source context, use **GitHub MCP tools** at your discretion (browse files,
   search, commits, etc.). The local workspace is already a clone of the configured branch; you may also
   use `workspace_list_files`, `workspace_read_file`, and `workspace_write_file` under that clone.
3. Apply **minimal, correct fixes** with `workspace_write_file` (or equivalent) so changes land in the
   local clone.
4. When the fix is ready, use **GitHub MCP tools** to publish: branch, push, and open a **pull request**.
   The PR **base** branch must be **exactly** the branch named in the user message (the same as
   `RUMMAGER_GIT_BRANCH` / the local clone)—**do not** use `main` or the repo default unless that branch
   name was explicitly given. GitHub authentication is handled by the GitHub MCP server when used that way.

If this process also has a **Kubernetes MCP** tool group, use it only when extra cluster context helps.

Constraints:
- Prefer small, reviewable changes; do not refactor unrelated code.
- Do not commit secrets or credentials.
- After you believe the fix is complete, summarize root cause and changes in plain language (include the
  PR link if you have it).
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
    label_name: str,
    label_value: str,
) -> str:
    logs_blob = snapshot.combined_log if snapshot.combined_log.strip() else "(no log lines collected)"
    return f"""## Kubernetes workload

Namespace: `{snapshot.namespace}`
Pod: `{snapshot.pod_name}` (UID `{snapshot.pod_uid}`, phase `{snapshot.phase}`)
Selector: `{label_name}={label_value}`

### Collected pod logs
{logs_blob}

## Local repository
Path on disk: `{repo_path}`
Configured branch (checkout + suggested PR base): **`{base_branch}`**

Recent commits:
```
{git_summary}
```

Use **GitHub MCP** tools when you want to inspect or cross-check source beyond the clone. Use
`workspace_list_files` / `workspace_read_file` / `workspace_write_file` for edits under the clone.

When your changes are ready, open a pull request with suggested head branch name `{branch_hint}`.
The PR **base** (merge target) **must** be **`{base_branch}`**—not `main` or any other branch unless
`{base_branch}` is literally that name.

Then write a short summary of the root cause and the fix (include the PR link if you have it).
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

    try:
        clone_repository(src, ws, settings.github_token, settings.git_clone_depth)
    except Exception as e:
        logger.exception(
            "Clone failed for %s/%s: %s. For private repos set GITHUB_TOKEN; otherwise ensure the repo is public.",
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

    summary = git_repo_summary(ws)
    branch_hint = f"{settings.pr_branch_prefix}/pod-{snapshot.pod_name}"[:250]
    user_prompt = _build_user_prompt(
        snapshot,
        summary,
        ws,
        branch_hint,
        settings.git_branch,
        settings.pod_label_name,
        settings.pod_label_value,
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

    pr_url: str | None = None
    pr_via = "github_mcp"
    if settings.dry_run_no_pr:
        logger.info("RUMMAGER_DRY_RUN_NO_PR set; skipping PR creation")
        pr_via = "dry_run"
    elif settings.github_token:
        branch = branch_hint
        pr_title = f"fix(logs): error in pod {snapshot.pod_name} ({snapshot.namespace})"
        pr_body = (
            f"Automated fix proposal from **Rummager** (pod log monitor).\n\n"
            f"Pod: `{snapshot.namespace}/{snapshot.pod_name}` (UID `{snapshot.pod_uid}`)\n\n"
            f"### Model summary\n\n{llm_summary}\n"
        )
        try:
            pr_url = create_branch_commit_push_pr(
                ws,
                branch_name=branch,
                token=settings.github_token,
                owner=src.owner,
                repo=src.repo,
                title=pr_title,
                body=pr_body,
                base_branch=settings.git_branch.strip() or src.default_branch_hint,
                fetch_depth=settings.git_clone_depth,
            )
        except Exception:
            logger.exception("Failed to create pull request for %s/%s", src.owner, src.repo)
            state.mark_incident_processed(
                incident_id,
                {
                    "reason": "pr_failed",
                    "pod": snapshot.pod_name,
                    "namespace": snapshot.namespace,
                    "repository": f"{src.owner}/{src.repo}",
                    "model_summary_excerpt": llm_summary[:8000],
                },
            )
            return
        pr_via = "github_rest"
    else:
        logger.info(
            "GITHUB_TOKEN unset: skipping in-app PR/push; expecting GitHub MCP to have opened a PR if needed"
        )

    logger.info("Pull request result: %s", pr_url or "(none from app; see GitHub MCP / model summary)")
    state.mark_incident_processed(
        incident_id,
        {
            "pod": snapshot.pod_name,
            "namespace": snapshot.namespace,
            "repository": f"{src.owner}/{src.repo}",
            "pull_request": pr_url,
            "pr_via": pr_via,
        },
    )


def run_forever(settings: Settings, state: StateStore) -> None:
    load_kube_config()
    src = git_source_from_clone_url(settings.git_clone_url, settings.git_branch)
    if not src:
        raise RuntimeError(
            "Could not derive GitHub owner/repo from RUMMAGER_GIT_CLONE_URL; check the URL format."
        )

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
