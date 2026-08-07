"""Platform-side git operations behind the two egress tools (contract C3).

`repo.archive` clones the target repo with the platform token, strips the
remote (and with it the credentialed URL), and returns the workspace as a
base64 tarball. `git.open_pr` is the only path by which code leaves the
system: verify the agent's bundle, apply it to a fresh platform-side clone,
extract the changed paths (fed to policy as `args.paths`), push the branch
with the platform token, and open the PR via the GitHub REST API.

The GIT_TOKEN only ever appears in the clone URL of throwaway platform-side
clones and in the GitHub API header; it is redacted from error messages and
never reaches archives, bundles, or audit events.
"""

import asyncio
import base64
import binascii
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from common.telemetry import get_logger
from gateway.config import GatewaySettings

GIT_TIMEOUT_S = 300
# The bundle branch lands on a private ref first, so fetching never collides
# with local branches (e.g. a checked-out `main`); policy decides afterwards
# whether it may be pushed out under its real name.
INCOMING_REF = "refs/poseidon/incoming"


class GitBackendError(Exception):
    pass


def repo_slug(repo: str) -> str:
    """`owner/name` slug for the GitHub API, from a repo URL or host path."""
    path = repo.split("://", 1)[-1].rstrip("/").removesuffix(".git")
    return "/".join(path.split("/")[-2:])


def _require_safe_argument(what: str, value: str) -> str:
    """Refuse caller-supplied strings that could be read as git options or
    smuggle transport tricks: leading dash, whitespace/control characters,
    and `::` (git's `ext::`/`fd::` pseudo-URLs execute commands)."""
    if not value or value.startswith("-") or "::" in value:
        raise GitBackendError(f"{what} {value!r} is not allowed")
    if any(c.isspace() or ord(c) < 0x20 for c in value):
        raise GitBackendError(f"{what} {value!r} is not allowed")
    return value


def _require_safe_ref(what: str, value: str) -> str:
    """A conservative subset of git's ref-format rules — enough that a
    caller-supplied ref or branch can never be an option or rewrite a
    refspec (`:` splits refspecs; `..` builds ranges)."""
    _require_safe_argument(what, value)
    if ":" in value or ".." in value or value.startswith("/") or value.endswith("/"):
        raise GitBackendError(f"{what} {value!r} is not a valid ref name")
    return value


@dataclass
class PreparedBundle:
    """A verified bundle applied to a platform-side clone, awaiting push."""

    branch: str
    paths: list[str]
    clone_dir: Path
    root: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class GitBackend:
    def __init__(self, settings: GatewaySettings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http_client
        self._log = get_logger("gateway.git")

    # -- helpers ------------------------------------------------------------

    def _redact(self, text: str) -> str:
        token = self._settings.git_token
        return text.replace(token, "***") if token else text

    def _git(self, cwd: Path | None, *args: str) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_S,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired:
            # TimeoutExpired's own message carries the full command line —
            # including a credentialed clone URL — so it never propagates
            # unredacted.
            raise GitBackendError(
                self._redact(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s")
            ) from None
        if proc.returncode != 0:
            raise GitBackendError(
                self._redact(f"git {args[0]} failed: {proc.stderr.strip()}")
            )
        return proc.stdout

    def _clone_url(self, repo: str) -> str:
        """Resolve `repo` to a clone URL. The transport set is closed:
        local paths (tests, fixtures) or https with the platform token —
        anything else is refused, because git's other transports (`ext::`
        among them) can execute commands."""
        _require_safe_argument("repo", repo)
        if repo.startswith(("file://", "/", ".")):
            return repo
        scheme, sep, _ = repo.partition("://")
        if sep and scheme != "https":
            raise GitBackendError(
                f"repo URL scheme {scheme!r} is not allowed (https only)"
            )
        token = self._settings.git_token
        if not token:
            raise GitBackendError("GIT_TOKEN is not configured")
        host_path = repo.split("://", 1)[-1]
        return f"https://x-access-token:{token}@{host_path}"

    # -- repo.archive ---------------------------------------------------------

    async def archive(self, repo: str, ref: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._archive_sync, repo, ref)

    def _archive_sync(self, repo: str, ref: str) -> dict[str, Any]:
        _require_safe_ref("ref", ref)
        self._log.info("archive", extra={"repo": repo_slug(repo), "ref": ref})
        with tempfile.TemporaryDirectory(prefix="poseidon-archive-") as tmp:
            clone = Path(tmp) / "repo"
            self._git(None, "clone", "--", self._clone_url(repo), str(clone))
            # `<ref> --` pins the argument as a revision, never an option
            # or a path.
            self._git(clone, "checkout", ref, "--")
            # Removing the remote drops the credentialed URL and all
            # remote-tracking refs; nothing secret survives in the archive.
            self._git(clone, "remote", "remove", "origin")
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                for child in sorted(clone.iterdir()):
                    tar.add(child, arcname=child.name)
            return {"archive_b64": base64.b64encode(buf.getvalue()).decode()}

    # -- git.open_pr ----------------------------------------------------------

    async def prepare_pr(self, repo: str, branch: str, bundle_b64: str) -> PreparedBundle:
        """Verify the bundle and apply it to a fresh platform-side clone.

        Runs before the policy decision so the changed paths can be part of
        the C2 policy input; nothing here leaves the gateway host.
        """
        return await asyncio.to_thread(self._prepare_sync, repo, branch, bundle_b64)

    def _prepare_sync(self, repo: str, branch: str, bundle_b64: str) -> PreparedBundle:
        _require_safe_ref("branch", branch)
        root = Path(tempfile.mkdtemp(prefix="poseidon-pr-"))
        try:
            bundle_path = root / "incoming.bundle"
            try:
                bundle_path.write_bytes(base64.b64decode(bundle_b64, validate=True))
            except binascii.Error as exc:
                raise GitBackendError(f"bundle is not valid base64: {exc}") from exc
            clone = root / "repo"
            self._git(None, "clone", "--", self._clone_url(repo), str(clone))
            self._git(clone, "bundle", "verify", str(bundle_path))
            self._git(clone, "fetch", str(bundle_path), f"{branch}:{INCOMING_REF}")
            diff = self._git(
                clone,
                "diff",
                "--name-only",
                f"origin/{self._settings.base_ref}...{INCOMING_REF}",
            )
            paths = [line for line in diff.splitlines() if line]
            self._log.info("bundle prepared", extra={"branch": branch, "paths": paths})
            return PreparedBundle(branch=branch, paths=paths, clone_dir=clone, root=root)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

    async def open_pr(
        self, prepared: PreparedBundle, repo: str, title: str, body: str
    ) -> dict[str, Any]:
        """Push the verified branch with the platform token and open the PR."""
        await asyncio.to_thread(
            self._git,
            prepared.clone_dir,
            "push",
            "origin",
            f"{INCOMING_REF}:refs/heads/{prepared.branch}",
        )
        data = await self._github(
            "POST",
            f"/repos/{repo_slug(repo)}/pulls",
            json={
                "title": title,
                "body": body,
                "head": prepared.branch,
                "base": self._settings.base_ref,
            },
        )
        pr_url = data["html_url"]
        self._log.info("pr opened", extra={"branch": prepared.branch, "pr_url": pr_url})
        return {"pr_url": pr_url}

    # -- GitHub REST (issues.*, git.read/comment/merge_pr) --------------------

    async def _github(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> Any:
        headers = {"Accept": "application/vnd.github+json"}
        if self._settings.git_token:
            headers["Authorization"] = f"Bearer {self._settings.git_token}"
        resp = await self._http.request(
            method,
            f"{self._settings.github_api_url.rstrip('/')}{path}",
            json=json,
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            raise GitBackendError(
                self._redact(f"GitHub {method} {path} failed:"
                             f" {resp.status_code} {resp.text}")
            )
        return resp.json()

    @staticmethod
    def _issue_view(issue: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": issue["number"],
            "title": issue.get("title", ""),
            "state": issue.get("state", ""),
            "body": issue.get("body") or "",
            "url": issue.get("html_url", ""),
        }

    async def create_issue(self, repo: str, title: str, body: str) -> dict[str, Any]:
        issue = await self._github(
            "POST", f"/repos/{repo_slug(repo)}/issues",
            json={"title": title, "body": body},
        )
        return self._issue_view(issue)

    async def read_issues(self, repo: str, number: int | None) -> dict[str, Any]:
        """One issue by number, or the open backlog — the PM's cross-run
        state is this list plus the runs history."""
        slug = repo_slug(repo)
        if number is not None:
            return {"issues": [self._issue_view(
                await self._github("GET", f"/repos/{slug}/issues/{number}")
            )]}
        listing = await self._github("GET", f"/repos/{slug}/issues?state=open")
        # The issues API returns PRs too; the backlog is issues only.
        return {"issues": [
            self._issue_view(i) for i in listing if "pull_request" not in i
        ]}

    async def update_issue(
        self,
        repo: str,
        number: int,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        fields = {
            k: v
            for k, v in (("title", title), ("body", body), ("state", state))
            if v is not None
        }
        if not fields:
            raise GitBackendError("issues.update: nothing to update")
        issue = await self._github(
            "PATCH", f"/repos/{repo_slug(repo)}/issues/{number}", json=fields
        )
        return self._issue_view(issue)

    async def comment_issue(self, repo: str, number: int, body: str) -> dict[str, Any]:
        """One endpoint serves both `issues.comment` and `git.comment` —
        GitHub PR conversations are issue comments."""
        comment = await self._github(
            "POST", f"/repos/{repo_slug(repo)}/issues/{number}/comments",
            json={"body": body},
        )
        return {"url": comment.get("html_url", "")}

    async def read_pr(self, repo: str, number: int) -> dict[str, Any]:
        """PR metadata + changed files — what a reviewer reads."""
        slug = repo_slug(repo)
        pr = await self._github("GET", f"/repos/{slug}/pulls/{number}")
        files = await self._github("GET", f"/repos/{slug}/pulls/{number}/files")
        return {
            "number": pr["number"],
            "title": pr.get("title", ""),
            "state": pr.get("state", ""),
            "body": pr.get("body") or "",
            "url": pr.get("html_url", ""),
            "head": (pr.get("head") or {}).get("ref", ""),
            "base": (pr.get("base") or {}).get("ref", ""),
            "merged": bool(pr.get("merged")),
            "files": [
                {
                    "path": f.get("filename", ""),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "patch": f.get("patch", ""),
                }
                for f in files
            ],
        }

    async def merge_pr(
        self, repo: str, number: int, method: str = "merge"
    ) -> dict[str, Any]:
        result = await self._github(
            "PUT", f"/repos/{repo_slug(repo)}/pulls/{number}/merge",
            json={"merge_method": method},
        )
        self._log.info("pr merged", extra={"repo": repo_slug(repo), "number": number})
        return {"merged": bool(result.get("merged")), "sha": result.get("sha", "")}
