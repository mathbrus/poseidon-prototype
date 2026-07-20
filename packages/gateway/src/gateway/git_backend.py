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
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if proc.returncode != 0:
            raise GitBackendError(
                self._redact(f"git {args[0]} failed: {proc.stderr.strip()}")
            )
        return proc.stdout

    def _clone_url(self, repo: str) -> str:
        if repo.startswith(("file://", "/", ".")):
            return repo
        token = self._settings.git_token
        if not token:
            raise GitBackendError("GIT_TOKEN is not configured")
        host_path = repo.split("://", 1)[-1]
        return f"https://x-access-token:{token}@{host_path}"

    # -- repo.archive ---------------------------------------------------------

    async def archive(self, repo: str, ref: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._archive_sync, repo, ref)

    def _archive_sync(self, repo: str, ref: str) -> dict[str, Any]:
        self._log.info("archive", extra={"repo": repo_slug(repo), "ref": ref})
        with tempfile.TemporaryDirectory(prefix="poseidon-archive-") as tmp:
            clone = Path(tmp) / "repo"
            self._git(None, "clone", self._clone_url(repo), str(clone))
            self._git(clone, "checkout", ref)
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
        root = Path(tempfile.mkdtemp(prefix="poseidon-pr-"))
        try:
            bundle_path = root / "incoming.bundle"
            try:
                bundle_path.write_bytes(base64.b64decode(bundle_b64, validate=True))
            except binascii.Error as exc:
                raise GitBackendError(f"bundle is not valid base64: {exc}") from exc
            clone = root / "repo"
            self._git(None, "clone", self._clone_url(repo), str(clone))
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
        headers = {"Accept": "application/vnd.github+json"}
        if self._settings.git_token:
            headers["Authorization"] = f"Bearer {self._settings.git_token}"
        resp = await self._http.post(
            f"{self._settings.github_api_url.rstrip('/')}/repos/{repo_slug(repo)}/pulls",
            json={
                "title": title,
                "body": body,
                "head": prepared.branch,
                "base": self._settings.base_ref,
            },
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            raise GitBackendError(
                self._redact(f"GitHub PR creation failed: {resp.status_code} {resp.text}")
            )
        pr_url = resp.json()["html_url"]
        self._log.info("pr opened", extra={"branch": prepared.branch, "pr_url": pr_url})
        return {"pr_url": pr_url}
