"""How a repository URL names its repository on the git host: the
gateway addresses GitHub's API by it, and the CLI asks a person to type
it before a purge, so the name confirmed is the name reset."""


def repo_slug(repo: str) -> str:
    """`owner/name`, from a repository URL or host path."""
    path = repo.split("://", 1)[-1].rstrip("/").removesuffix(".git")
    return "/".join(path.split("/")[-2:])
