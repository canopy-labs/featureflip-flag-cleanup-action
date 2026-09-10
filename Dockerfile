# python:3.12-slim — Python 3.12 specifically: the pinned polyglot-piranha is
# installed from a prebuilt cp312 wheel fetched into wheels/ before this build
# (see pyproject.toml for why pip cannot resolve it from PyPI), so this base
# image and the fetched wheel's python tag move together or not at all.
#
# As of this writing python:3.12-slim is Debian 13 "trixie", whose apt
# archive carries git 2.47.3 — comfortably over the git >= 2.31 floor that
# git_ops.push_branch's GIT_CONFIG_COUNT/GIT_CONFIG_KEY_n/GIT_CONFIG_VALUE_n
# auth mechanism requires (older git silently ignores those env vars, so the
# push would fail as an opaque 403, not a version error). Re-verify after any
# base image bump:
#   docker run --rm python:3.12-slim git --version
FROM python:3.12-slim

# git: required to branch/commit/push each flag's removal PR (git_ops.py).
# Deliberately no Node/npm here — the syntax-safety gates in ts_syntax.py use
# tree-sitter (pure Python wheels) precisely so this image needs no JS
# runtime at all.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# GitHub Docker actions run as root, but the checkout (actions/checkout) is
# owned by the runner user — a UID mismatch git >= 2.35.2 refuses to operate
# on ("detected dubious ownership in repository") unless the path is
# explicitly trusted. `--system` (not `--global`) so the trust doesn't depend
# on $HOME matching at run time. Trusting every path is safe here because the
# only repository this container ever touches is the one the workflow itself
# mounted in as the checkout.
RUN git config --system --add safe.directory '*'

WORKDIR /action

# Install from pyproject.toml so the dependency set (polyglot-piranha, httpx,
# tree-sitter, tree-sitter-typescript) stays single-sourced. Do not hardcode
# a duplicate package list here — pyproject.toml is the one place versions
# are pinned/floated, and it changes independently of this file.
#
# LICENSE travels with the code: this image ships Apache-2.0 software, and
# section 4(a) requires the licence text to accompany any copy of it. The
# public wrapper repository gets its own copy; the container needs one too.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# The polyglot-piranha pin resolves ONLY from wheels/ — that version exists
# as a GitHub-release wheel, not on PyPI (see pyproject.toml). wheels/ is
# filled by the fetch script under scripts/ before any build: CI and the
# dogfood workflow each run it, and a local build needs it run once too. A
# bind mount rather than a COPY keeps the .whl out of the image's layers —
# site-packages gets the installed package and nothing ships the wheel file
# itself. `RUN --mount` needs BuildKit, the default builder everywhere this
# image is built (docker >= 23 locally and on the runner, buildx in the
# publish workflow).
RUN --mount=type=bind,source=wheels,target=/wheels \
    pip install --no-cache-dir --find-links /wheels .

ENTRYPOINT ["python", "-m", "flag_cleanup"]
