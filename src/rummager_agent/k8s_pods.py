from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)


def load_kube_config() -> None:
    try:
        config.load_incluster_config()
        logger.info("Using in-cluster Kubernetes configuration")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Using local kubeconfig")


@dataclass
class PodLogSnapshot:
    """Logs from one pod (optionally multiple containers)."""

    namespace: str
    pod_name: str
    pod_uid: str
    phase: str
    container_logs: list[str] = field(default_factory=list)

    @property
    def combined_log(self) -> str:
        return "\n\n".join(self.container_logs) if self.container_logs else ""


def _pod_label_selector(label_name: str, label_value: str) -> str:
    name = label_name.strip()
    value = label_value.strip()
    if not name:
        raise ValueError("pod label name must be non-empty")
    # RFC 1123-ish: values are often alphanum; escape if needed for API
    return f"{name}={value}"


def list_pods_matching_label(namespace: str, label_name: str, label_value: str) -> list[Any]:
    load_kube_config()
    v1 = client.CoreV1Api()
    selector = _pod_label_selector(label_name, label_value)
    try:
        resp = v1.list_namespaced_pod(namespace, label_selector=selector)
    except ApiException as e:
        logger.warning("Could not list pods in %s with %s: %s", namespace, selector, e)
        return []
    return list(resp.items or [])


def _read_container_log(
    namespace: str,
    pod_name: str,
    container_name: str,
    tail_lines: int,
    max_bytes: int,
    since_seconds: int | None = None,
) -> str:
    v1 = client.CoreV1Api()
    kwargs: dict[str, Any] = {
        "name": pod_name,
        "namespace": namespace,
        "container": container_name,
        "tail_lines": tail_lines,
    }
    if since_seconds is not None:
        kwargs["since_seconds"] = since_seconds
    try:
        raw = v1.read_namespaced_pod_log(**kwargs)
    except ApiException as e:
        return f"(failed to read logs for container {container_name}: {e})"
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    encoded = raw.encode("utf-8")
    if len(encoded) > max_bytes:
        raw = encoded[-max_bytes:].decode("utf-8", errors="replace")
        raw = "--- truncated ---\n" + raw
    return raw


def collect_pod_logs(
    namespace: str,
    pod: Any,
    tail_lines: int,
    max_bytes_per_container: int,
    since_seconds: int | None = None,
) -> PodLogSnapshot:
    md = pod.metadata
    name = md.name or "unknown"
    uid = md.uid or name
    phase = (pod.status.phase or "?") if pod.status else "?"

    containers: list[str] = []
    spec = pod.spec
    if spec:
        for c in spec.containers or []:
            if c.name:
                containers.append(c.name)
        for c in spec.init_containers or []:
            if c.name:
                containers.append(c.name)

    chunks: list[str] = []
    for cname in containers:
        body = _read_container_log(
            namespace,
            name,
            cname,
            tail_lines,
            max_bytes_per_container,
            since_seconds=since_seconds,
        )
        chunks.append(f"### Container `{cname}`\n{body}")

    return PodLogSnapshot(
        namespace=namespace,
        pod_name=name,
        pod_uid=uid,
        phase=phase,
        container_logs=chunks,
    )


def log_has_error_indicators(
    log_text: str,
    substrings: list[str],
    regex: re.Pattern[str] | None,
) -> bool:
    if not log_text.strip():
        return False
    if regex is not None and regex.search(log_text):
        return True
    lower = log_text.lower()
    for s in substrings:
        t = s.strip()
        if not t:
            continue
        if t.lower() in lower:
            return True
    return False


def excerpt_for_fingerprint(log_text: str, max_chars: int = 12000) -> str:
    """Stable-ish excerpt for deduplication (tail of log)."""
    t = log_text.strip()
    if len(t) <= max_chars:
        return t
    return t[-max_chars:]
