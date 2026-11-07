"""Which forge this run talks to, and as whom, read from the CI it runs in.

Two things vary independently: the CI provider the image runs in, and the forge
the repository is hosted on. A reader per CI provider turns that provider's
environment into a :class:`RunContext`, and :func:`forge_for` turns the context
into a :class:`~flag_cleanup.forge.Forge`. Any CI provider without a reader of
its own names the forge explicitly with ``FLAG_CLEANUP_*`` variables.

Precedence, first match wins:

1. ``FLAG_CLEANUP_FORGE`` is set: the explicit, provider-neutral form.
2. ``GITHUB_ACTIONS=true``: GitHub Actions.
3. ``GITHUB_TOKEN`` and ``GITHUB_REPOSITORY`` are both set: GitHub, from any
   CI that exports GitHub's variables itself (including GitLab CI or
   Bitbucket Pipelines building a repository hosted on GitHub).
4. ``GITLAB_CI=true``: GitLab CI.
5. ``BITBUCKET_BUILD_NUMBER`` is set: Bitbucket Pipelines.
6. Otherwise GitHub, whose own configuration error names what to pass.

With ``FLAG_CLEANUP_FORGE=github``, each ``FLAG_CLEANUP_*`` value falls back
to GitHub's own variable of the same meaning, so an override never pairs one
host's token with another host's URLs.

``FLAG_CLEANUP_FORGE_TOKEN``, when set, is the token on every forge.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from flag_cleanup.forge import Forge, ForgeConfigError
from flag_cleanup.github_forge import github_forge
from flag_cleanup.github_ops import GitHubApi, GitHubEnv

FORGES = ("github", "gitlab", "bitbucket")

#: The public cloud's API and web origins, used when nothing overrides them.
_DEFAULT_URLS = {
    "github": ("https://api.github.com", "https://github.com"),
    "gitlab": ("https://gitlab.com/api/v4", "https://gitlab.com"),
    "bitbucket": ("https://api.bitbucket.org/2.0", "https://bitbucket.org"),
}

FORGE_TOKEN = "FLAG_CLEANUP_FORGE_TOKEN"


@dataclass(frozen=True)
class RunContext:
    """The forge, the repository on it and the credential, from one CI environment."""

    #: ``"github"``, ``"gitlab"`` or ``"bitbucket"``.
    forge: str
    #: Which reader produced this: ``"github-actions"``, ``"gitlab-ci"``,
    #: ``"bitbucket-pipelines"``, ``"generic"`` or ``"legacy"``.
    ci: str
    #: The repository's PATH on the forge, never a numeric id: the push
    #: remote is built from it.
    repository: str
    api_url: str
    server_url: str
    token: str = field(repr=False)


def from_env(env: Mapping[str, str] | None = None) -> RunContext:
    """Read the run's forge from ``env`` (the process environment by default)."""
    env = os.environ if env is None else env
    if env.get("FLAG_CLEANUP_FORGE"):
        return _generic(env)
    if env.get("GITHUB_ACTIONS") == "true":
        return _github(env, ci="github-actions")
    # Before the native GitLab and Bitbucket readers: GitHub's own pair, set by
    # hand, has always meant a GitHub repository whatever CI it runs in.
    if env.get("GITHUB_TOKEN") and env.get("GITHUB_REPOSITORY"):
        return _github(env, ci="legacy")
    if env.get("GITLAB_CI") == "true":
        return _gitlab(env)
    if env.get("BITBUCKET_BUILD_NUMBER"):
        return _bitbucket(env)
    # Anything else is read as GitHub, as it always has been: a CI that exports
    # GitHub's variables itself, or a run missing them, which then fails with
    # GitHub's own message naming what to pass.
    return _github(env, ci="legacy")


def detect_forge(env: Mapping[str, str] | None = None) -> str | None:
    """Which forge ``env`` names, by :func:`from_env`'s precedence, checking no credential.

    ``None`` for an override that names no known forge. For deciding what a run
    may do before anything is validated: a run refused for its forge should say
    so, not that it lacks a token.
    """
    env = os.environ if env is None else env
    override = env.get("FLAG_CLEANUP_FORGE")
    if override:
        name = override.strip().lower()
        return name if name in FORGES else None
    if env.get("GITHUB_ACTIONS") == "true":
        return "github"
    if env.get("GITHUB_TOKEN") and env.get("GITHUB_REPOSITORY"):
        return "github"
    if env.get("GITLAB_CI") == "true":
        return "gitlab"
    if env.get("BITBUCKET_BUILD_NUMBER"):
        return "bitbucket"
    return "github"


def forge_for(ctx: RunContext) -> Forge:
    """The :class:`~flag_cleanup.forge.Forge` for ``ctx``, with its own client."""
    if ctx.forge == "github":
        return github_forge(github_env(ctx))
    if ctx.forge == "gitlab":
        # Imported here: the GitLab forge reads this module's RunContext.
        from flag_cleanup.gitlab_forge import GitLabApi, GitLabForge

        return GitLabForge(GitLabApi(ctx.token, api_url=ctx.api_url), ctx, owns_api=True)
    if ctx.forge == "bitbucket":
        # Imported here for the same reason.
        from flag_cleanup.bitbucket_forge import BitbucketApi, BitbucketForge

        return BitbucketForge(BitbucketApi(ctx.token, api_url=ctx.api_url), ctx, owns_api=True)
    raise ForgeConfigError(f"{ctx.forge} is not supported by this version")


def github_env_from(env: Mapping[str, str] | None = None) -> GitHubEnv:
    """GitHub's run settings for a mode only GitHub can serve.

    The same object ``GitHubEnv.from_env`` returns, except that
    ``FLAG_CLEANUP_FORGE_TOKEN`` stands in for ``GITHUB_TOKEN`` here as it does
    everywhere else. A run on another forge is refused by name.
    """
    detected = detect_forge(env)
    if detected not in (None, "github"):
        raise ForgeConfigError(f"this mode needs GitHub, but the run is on {detected}")
    return github_env(from_env(env))


def resolve_forge(
    *,
    forge: Forge | None = None,
    github: GitHubEnv | None = None,
    gh: GitHubApi | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Forge, bool]:
    """The forge an entry point runs against, and whether the caller must close it.

    An explicit ``forge`` wins. A GitHub environment or client passed the way
    entry points have always accepted them is wrapped as GitHub. Otherwise the
    forge is read from the environment.
    """
    if forge is not None:
        return forge, False
    if github is not None or gh is not None:
        resolved = github if github is not None else GitHubEnv.from_env(env)
        return github_forge(resolved, gh), True
    return forge_for(from_env(env)), True


def github_env(ctx: RunContext) -> GitHubEnv:
    """``ctx`` as the GitHub module's own environment type."""
    if ctx.forge != "github":
        raise ForgeConfigError(f"expected a GitHub run, got {ctx.forge}")
    return GitHubEnv(
        token=ctx.token,
        repository=ctx.repository,
        api_url=ctx.api_url,
        server_url=ctx.server_url,
    )


def _github(env: Mapping[str, str], *, ci: str) -> RunContext:
    # GitHubEnv owns GitHub's validation and its messages; a forge token only
    # stands in for GITHUB_TOKEN.
    if env.get(FORGE_TOKEN):
        env = {**env, "GITHUB_TOKEN": env[FORGE_TOKEN]}
    github = GitHubEnv.from_env(env)
    return RunContext(
        forge="github",
        ci=ci,
        repository=github.repository,
        api_url=github.api_url,
        server_url=github.server_url,
        token=github.token,
    )


def _gitlab(env: Mapping[str, str]) -> RunContext:
    # CI_JOB_TOKEN is deliberately not read: it cannot open merge requests.
    token = env.get(FORGE_TOKEN) or env.get("GITLAB_TOKEN") or ""
    missing = [name for name in ("CI_PROJECT_PATH",) if not env.get(name)]
    if not token:
        missing.append(f"GITLAB_TOKEN (or {FORGE_TOKEN})")
    if missing:
        raise ForgeConfigError(
            f"missing required environment variable(s): {', '.join(missing)} "
            "(the token is a project or group access token with the api scope; "
            "CI_JOB_TOKEN cannot open merge requests)"
        )
    api_default, server_default = _DEFAULT_URLS["gitlab"]
    return _checked(
        RunContext(
            forge="gitlab",
            ci="gitlab-ci",
            repository=env["CI_PROJECT_PATH"],
            api_url=env.get("CI_API_V4_URL") or api_default,
            server_url=env.get("CI_SERVER_URL") or server_default,
            token=token,
        )
    )


def _bitbucket(env: Mapping[str, str]) -> RunContext:
    token = env.get(FORGE_TOKEN) or env.get("BITBUCKET_ACCESS_TOKEN") or ""
    missing = [
        name for name in ("BITBUCKET_WORKSPACE", "BITBUCKET_REPO_SLUG") if not env.get(name)
    ]
    if not token:
        missing.append(f"BITBUCKET_ACCESS_TOKEN (or {FORGE_TOKEN})")
    if missing:
        raise ForgeConfigError(
            f"missing required environment variable(s): {', '.join(missing)} "
            "(the token is a repository access token)"
        )
    api_url, server_url = _DEFAULT_URLS["bitbucket"]
    return _checked(
        RunContext(
            forge="bitbucket",
            ci="bitbucket-pipelines",
            repository=f"{env['BITBUCKET_WORKSPACE']}/{env['BITBUCKET_REPO_SLUG']}",
            api_url=api_url,
            server_url=server_url,
            token=token,
        )
    )


def _generic(env: Mapping[str, str]) -> RunContext:
    forge = env["FLAG_CLEANUP_FORGE"].strip().lower()
    if forge not in FORGES:
        raise ForgeConfigError(
            f"FLAG_CLEANUP_FORGE must be one of {', '.join(FORGES)}, got {forge!r}"
        )
    if forge == "github":
        # Same validation as on Actions, with the generic names mapped onto it.
        mapped = {
            "GITHUB_TOKEN": env.get(FORGE_TOKEN) or env.get("GITHUB_TOKEN") or "",
            "GITHUB_REPOSITORY": env.get("FLAG_CLEANUP_REPOSITORY")
            or env.get("GITHUB_REPOSITORY")
            or "",
            "GITHUB_API_URL": env.get("FLAG_CLEANUP_API_URL")
            or env.get("GITHUB_API_URL")
            or "",
            "GITHUB_SERVER_URL": env.get("FLAG_CLEANUP_SERVER_URL")
            or env.get("GITHUB_SERVER_URL")
            or "",
        }
        return _github(mapped, ci="generic")
    missing = [
        name for name in ("FLAG_CLEANUP_REPOSITORY", FORGE_TOKEN) if not env.get(name)
    ]
    if missing:
        raise ForgeConfigError(
            f"missing required environment variable(s): {', '.join(missing)}"
        )
    api_url = env.get("FLAG_CLEANUP_API_URL") or ""
    server_url = env.get("FLAG_CLEANUP_SERVER_URL") or ""
    if bool(api_url) != bool(server_url):
        # One set and one defaulted would pair a self-managed instance's
        # token with the public cloud's other half: API calls or the push
        # would carry it to a host it was never meant for.
        raise ForgeConfigError(
            "set FLAG_CLEANUP_API_URL and FLAG_CLEANUP_SERVER_URL together, or "
            f"neither (for {forge}'s public cloud); only one of them is set"
        )
    api_default, server_default = _DEFAULT_URLS[forge]
    return _checked(
        RunContext(
            forge=forge,
            ci="generic",
            repository=env["FLAG_CLEANUP_REPOSITORY"],
            api_url=api_url or api_default,
            server_url=server_url or server_default,
            token=env[FORGE_TOKEN],
        )
    )


def _checked(ctx: RunContext) -> RunContext:
    parts = ctx.repository.strip("/").split("/")
    if ctx.forge == "gitlab" and ctx.repository.isdigit():
        raise ForgeConfigError(
            "the GitLab repository must be the project path (group/project), "
            f"not its numeric ID ({ctx.repository})"
        )
    if len(parts) < 2 or not all(parts):
        raise ForgeConfigError(
            f"the repository must be a path like owner/name, got {ctx.repository!r}"
        )
    if ctx.forge == "bitbucket" and len(parts) != 2:
        raise ForgeConfigError(
            f"a Bitbucket repository is workspace/slug, got {ctx.repository!r}"
        )
    return ctx
