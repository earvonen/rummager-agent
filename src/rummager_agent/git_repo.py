from __future__ import annotations

import logging
import re
import subprocess
import urllib.parse
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


def git_source_from_clone_url(clone_url: str, branch: str) -> GitSource | None:
    """Build GitSource from configured clone URL and branch (checkout + PR base)."""
    url = clone_url.strip()
    if not url:
        return None
    b = branch.strip()
    parsed = _owner_repo_from_clone_url(url)
    if not parsed:
        logger.warning("Could not parse owner/repo from RUMMAGER_GIT_CLONE_URL: %s", url)
        return None
    owner, repo = parsed
    return GitSource(
        owner=owner,
        repo=repo,
        clone_url=url,
        revision=b or None,
        default_branch_hint=b or None,
    )


def _owner_repo_from_clone_url(clone_url: str) -> tuple[str, str] | None:
    """Best-effort owner/repo for GitHub PRs and logging; supports https and git@ GitHub, else URL path tail."""
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


def _authenticated_clone_url(clone_url: str, token: str | None) -> str:
    if not token:
        return clone_url
    if not clone_url.startswith("https://github.com/"):
        return clone_url
    rest = clone_url.removeprefix("https://")
    user = urllib.parse.quote("x-access-token", safe="")
    tok = urllib.parse.quote(token, safe="")
    return f"https://{user}:{tok}@{rest}"


def clone_repository(
    source: GitSource,
    dest: Path,
    token: str | None,
    depth: int,
) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"Workspace not empty: {dest}")

    url = _authenticated_clone_url(source.clone_url, token)
    rev = (source.revision or "").strip()
    if rev:
        logger.info("Cloning %s into %s (branch/ref %r, shallow)", source.clone_url, dest, rev)
        try:
            # Shallow single-branch clone so HEAD is the configured branch, not the repo default (e.g. main).
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


def create_branch_commit_push_pr(
    repo_path: Path,
    branch_name: str,
    token: str,
    owner: str,
    repo: str,
    title: str,
    body: str,
    base_branch: str | None,
) -> str:
    """
    Commit all changes, push branch, open a pull request via GitHub REST API.
    """
    r = Repo(repo_path)
    with r.config_writer() as cw:
        cw.set_value("user", "name", "rummager-agent")
        cw.set_value("user", "email", "rummager-agent@users.noreply.openshift.local")
    configured_base = (base_branch or "").strip() or None

    if r.is_dirty(untracked_files=True):
        # Feature branch must start from the configured merge base, not whatever default clone used.
        if configured_base:
            try:
                r.git.checkout(configured_base)
            except Exception:
                try:
                    r.git.checkout(f"origin/{configured_base}")
                except Exception as e:
                    logger.warning(
                        "Could not checkout PR base %r before creating %r (dirty tree may block switch): %s",
                        configured_base,
                        branch_name,
                        e,
                    )
        r.git.checkout("-b", branch_name)
        r.git.add(all=True)
        r.git.commit("-m", "fix: address error seen in pod logs (Rummager)")
    else:
        return "(no local changes; skipping PR)"

    auth_url = _authenticated_clone_url(f"https://github.com/{owner}/{repo}.git", token)
    r.git.remote("set-url", "origin", auth_url)
    r.git.push("--set-upstream", "origin", branch_name)

    import json
    import urllib.error
    import urllib.request

    base = configured_base
    if not base:
        try:
            base = r.git.rev_parse("--abbrev-ref", "origin/HEAD").replace("origin/", "")
        except Exception:
            base = "main"

    logger.info("GitHub PR: base=%r head=%r", base, branch_name)

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    payload = {"title": title, "body": body, "head": branch_name, "base": base}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return str(data.get("html_url", data))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub PR API error {e.code}: {err_body}") from e
