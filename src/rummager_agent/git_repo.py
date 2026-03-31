from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from git import Repo

logger = logging.getLogger(__name__)

_GITHUB_HTTPS = re.compile(
    r"https?://(?:[^@]+@)?github\.com/([^/]+)/([^/.]+)(?:\.git)?",
    re.I,
)
_GITHUB_SSH = re.compile(
    r"git@github\.com:([^/]+)/([^/.]+)(?:\.git)?\s*$",
    re.I,
)


@dataclass(frozen=True)
class GitSource:
    owner: str
    repo: str
    clone_url: str
    revision: str | None
    default_branch_hint: str | None


def parse_repository_slug(slug: str) -> tuple[str, str] | None:
    """Parse ``owner/repo`` (exactly one slash, two non-empty parts)."""
    s = slug.strip()
    if not s or s.count("/") != 1:
        return None
    owner, _, name = s.partition("/")
    owner, name = owner.strip(), name.strip()
    if not owner or not name or "/" in name:
        return None
    return owner, name


def resolve_git_source(
    git_repository: str | None,
    git_clone_url: str | None,
    git_branch: str,
) -> GitSource:
    """
    Resolve owner/repo from ``RUMMAGER_GIT_REPOSITORY`` and/or ``RUMMAGER_GIT_CLONE_URL``.

    ``clone_url`` may be empty for stack-only pods (no git network egress); GitHub MCP supplies source.
    """
    b = (git_branch or "").strip()
    url = (git_clone_url or "").strip()
    slug = (git_repository or "").strip()

    owner: str
    repo: str
    if slug:
        parsed = parse_repository_slug(slug)
        if not parsed:
            raise ValueError(
                "RUMMAGER_GIT_REPOSITORY must be exactly owner/repo (one slash, two non-empty parts)"
            )
        owner, repo = parsed
    elif url:
        parsed = _owner_repo_from_clone_url(url)
        if not parsed:
            raise ValueError(
                "Could not parse owner/repo from RUMMAGER_GIT_CLONE_URL; set RUMMAGER_GIT_REPOSITORY"
            )
        owner, repo = parsed
    else:
        raise ValueError("Set RUMMAGER_GIT_REPOSITORY (owner/repo) or RUMMAGER_GIT_CLONE_URL")

    return GitSource(
        owner=owner,
        repo=repo,
        clone_url=url,
        revision=b or None,
        default_branch_hint=b or None,
    )


def _owner_repo_from_clone_url(clone_url: str) -> tuple[str, str] | None:
    """Best-effort owner/repo for logging; supports GitHub https / git@, else URL path tail."""
    u = clone_url.strip()
    m = _GITHUB_HTTPS.search(u)
    if m:
        return m.group(1), m.group(2)
    m = _GITHUB_SSH.match(u)
    if m:
        return m.group(1), m.group(2)
    if u.startswith("git@"):
        tail = u.split(":", 1)[-1]
        parts = tail.replace(".git", "").strip("/").split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]
        return None
    path = urlparse(u).path.strip("/")
    parts = [x for x in path.split("/") if x]
    if len(parts) >= 2:
        return parts[-2], parts[-1].removesuffix(".git")
    return None


def clone_repository(
    source: GitSource,
    dest: Path,
    depth: int,
) -> Path:
    url = source.clone_url.strip()
    if not url:
        raise ValueError("clone_repository requires a non-empty clone URL")

    dest.mkdir(parents=True, exist_ok=True)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"Workspace not empty: {dest}")
    rev = (source.revision or "").strip()
    if rev:
        logger.info("Cloning %s into %s (branch/ref %r, shallow)", source.clone_url, dest, rev)
        try:
            Repo.clone_from(
                url,
                dest,
                depth=depth,
                branch=rev,
                single_branch=True,
            )
        except Exception as e:
            logger.warning(
                "Shallow clone with --branch %r failed (%s); falling back to default clone + checkout",
                rev,
                e,
            )
            repo = Repo.clone_from(url, dest, depth=depth)
            try:
                repo.git.fetch("origin", rev, depth=depth)
            except Exception as e2:
                logger.warning("Fetch %s failed: %s", rev, e2)
            try:
                repo.git.checkout(rev)
            except Exception:
                try:
                    repo.git.checkout("FETCH_HEAD")
                except Exception as e3:
                    logger.warning("Checkout %s failed: %s", rev, e3)
    else:
        logger.info("Cloning %s into %s (default branch)", source.clone_url, dest)
        Repo.clone_from(url, dest, depth=depth)

    return dest


STACK_ONLY_MARKER = ".rummager-stack-only"


def workspace_git_summary_for_prompt(repo_path: Path, source: GitSource) -> str:
    """Recent commits if cloned; otherwise instruct the model to use GitHub MCP only."""
    if not source.clone_url.strip():
        ref = source.default_branch_hint or "?"
        return (
            f"(no local git clone — this pod has no git clone; use **GitHub MCP** for all repository reads/search and issue creation "
            f"on repository **`{source.owner}/{source.repo}`**, branch/ref **`{ref}`**)"
        )
    return git_repo_summary(repo_path)


def git_repo_summary(repo_path: Path, max_lines: int = 200) -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_path), "log", "-n", "20", "--oneline"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, OSError) as e:
        return f"(git log failed: {e})"
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["..."]
    return "\n".join(lines)
