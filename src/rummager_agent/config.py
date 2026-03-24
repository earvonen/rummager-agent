from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import AliasChoices, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class McpRegistration:
    """Optional MCP registration applied at startup (Llama Stack toolgroups.register)."""

    toolgroup_id: str
    provider_id: str
    mcp_uri: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kubernetes_namespace: str = Field(
        ...,
        description="Namespace where labeled Pods are listed and logs are read",
        validation_alias=AliasChoices("RUMMAGER_KUBERNETES_NAMESPACE", "RUMMAGER_WATCH_NAMESPACE"),
    )
    pod_label_name: str = Field(
        ...,
        description="Pod label key used to select workloads (e.g. app)",
        validation_alias="RUMMAGER_POD_LABEL_NAME",
    )
    pod_label_value: str = Field(
        ...,
        description="Pod label value paired with RUMMAGER_POD_LABEL_NAME",
        validation_alias="RUMMAGER_POD_LABEL_VALUE",
    )

    git_clone_url: str = Field(
        ...,
        description="HTTPS or SSH clone URL for the application repository",
        validation_alias="RUMMAGER_GIT_CLONE_URL",
    )
    git_branch: str = Field(
        ...,
        description="Branch to check out locally and as PR merge base",
        validation_alias="RUMMAGER_GIT_BRANCH",
    )

    poll_interval_seconds: int = Field(120, validation_alias="RUMMAGER_POLL_INTERVAL_SECONDS")
    state_file_path: str = Field("/tmp/rummager-agent-state.json", validation_alias="RUMMAGER_STATE_FILE")

    llama_stack_base_url: str = Field(..., validation_alias="LLAMA_STACK_BASE_URL")
    llama_stack_api_key: str | None = Field(None, validation_alias="LLAMA_STACK_API_KEY")
    llama_stack_model_id: str | None = Field(None, validation_alias="LLAMA_STACK_MODEL_ID")

    tool_group_ids: str = Field(
        ...,
        description="Comma-separated Llama Stack tool group IDs (include GitHub MCP for repo browsing)",
        validation_alias="RUMMAGER_TOOL_GROUP_IDS",
    )

    mcp_registrations_json: str | None = Field(
        None,
        validation_alias="RUMMAGER_MCP_REGISTRATIONS_JSON",
        description='Optional JSON list: [{"toolgroup_id":"mcp::x","provider_id":"model-context-protocol","mcp_uri":"http://host/sse"}]',
    )

    github_token: str | None = Field(
        None,
        validation_alias="GITHUB_TOKEN",
        description="Optional: HTTPS clone of private repos + REST API PR fallback. "
        "If unset, use public clone only and rely on GitHub MCP (its own PAT) for PRs.",
    )
    git_clone_depth: int = Field(50, validation_alias="RUMMAGER_GIT_CLONE_DEPTH")
    workspace_root: str = Field("/tmp/rummager-workspaces", validation_alias="RUMMAGER_WORKSPACE_ROOT")

    max_llm_iterations: int = Field(40, validation_alias="RUMMAGER_MAX_LLM_ITERATIONS")
    log_tail_lines: int = Field(3000, validation_alias="RUMMAGER_LOG_TAIL_LINES")
    log_truncate_bytes: int = Field(65536, validation_alias="RUMMAGER_LOG_TRUNCATE_BYTES")
    log_truncate_bytes_per_container: int | None = Field(
        None,
        validation_alias="RUMMAGER_LOG_PER_CONTAINER_MAX_BYTES",
        description="If unset, RUMMAGER_LOG_TRUNCATE_BYTES is used per container",
    )
    log_max_age_seconds: int | None = Field(
        None,
        validation_alias="RUMMAGER_LOG_MAX_AGE_SECONDS",
        description="If set, only fetch container logs from the last N seconds (Kubernetes sinceSeconds). "
        "Unset = no time cutoff (still limited by RUMMAGER_LOG_TAIL_LINES and byte caps).",
    )

    error_log_substrings: str = Field(
        "ERROR,FATAL,Exception,Traceback,panic",
        validation_alias="RUMMAGER_ERROR_LOG_SUBSTRINGS",
        description="Comma-separated case-insensitive substrings; any match in logs triggers analysis",
    )
    error_log_regex: str | None = Field(
        None,
        validation_alias="RUMMAGER_ERROR_LOG_REGEX",
        description="If set, this regex is also applied to the log text (multi-line)",
    )

    pr_branch_prefix: str = Field("rummager-agent", validation_alias="RUMMAGER_PR_BRANCH_PREFIX")
    dry_run_no_pr: bool = Field(False, validation_alias="RUMMAGER_DRY_RUN_NO_PR")

    _compiled_error_regex: re.Pattern[str] | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _compile_regex(self) -> Settings:
        if self.error_log_regex and str(self.error_log_regex).strip():
            try:
                self._compiled_error_regex = re.compile(
                    self.error_log_regex.strip(),
                    re.MULTILINE | re.DOTALL,
                )
            except re.error as e:
                raise ValueError(f"RUMMAGER_ERROR_LOG_REGEX invalid: {e}") from e
        else:
            self._compiled_error_regex = None
        return self

    @property
    def compiled_error_regex(self) -> re.Pattern[str] | None:
        return self._compiled_error_regex

    @property
    def tool_group_id_list(self) -> list[str]:
        return [x.strip() for x in self.tool_group_ids.split(",") if x.strip()]

    @property
    def error_substring_list(self) -> list[str]:
        return [x.strip() for x in self.error_log_substrings.split(",") if x.strip()]

    @property
    def per_container_log_budget(self) -> int:
        return self.log_truncate_bytes_per_container or self.log_truncate_bytes

    @field_validator("log_max_age_seconds", mode="before")
    @classmethod
    def _log_max_age_optional(cls, v: Any) -> int | None:
        if v is None or v == "":
            return None
        return int(v)

    def parsed_mcp_registrations(self) -> list[McpRegistration]:
        if not self.mcp_registrations_json:
            return []
        raw: list[Any] = json.loads(self.mcp_registrations_json)
        out: list[McpRegistration] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("RUMMAGER_MCP_REGISTRATIONS_JSON must be a JSON list of objects")
            out.append(
                McpRegistration(
                    toolgroup_id=str(item["toolgroup_id"]),
                    provider_id=str(item.get("provider_id") or "model-context-protocol"),
                    mcp_uri=str(item["mcp_uri"]),
                )
            )
        return out

    @field_validator(
        "poll_interval_seconds",
        "git_clone_depth",
        "max_llm_iterations",
        "log_tail_lines",
        "log_truncate_bytes",
        "log_truncate_bytes_per_container",
        "log_max_age_seconds",
    )
    @classmethod
    def _positive_optional(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 1:
            raise ValueError("must be >= 1")
        return v
