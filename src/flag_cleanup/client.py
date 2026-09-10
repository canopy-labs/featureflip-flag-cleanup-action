"""Fetch removal candidates from the Featureflip public API.

Runs in the CUSTOMER's CI against the public ``/api/v1`` surface, authenticated
with THEIR ``FEATUREFLIP_API_TOKEN`` (see the package docstring). No
Featureflip-internal endpoint or secret is ever touched from here.

Endpoint contract (frozen — read off the shipped backend controller, not
inferred from the docs):

``GET {api_url}/api/v1/orgs/{org}/projects/{project}/flags/removal-candidates``
``?staleness=dead|stale`` (``&cursor=`` to page), ``Authorization: Bearer <token>``,
returning ``{"items": [{"key", "reason", "treatment", "status"}], "next_cursor": str | None}``.
``next_cursor`` is snake_case and ``status`` is PascalCase (``"Dead"``/``"Stale"``)
on the wire — both verbatim, not a guess.

``GET {api_url}/api/v1/orgs/{org}/projects/{project}/flags`` (``&cursor=`` to
page, NO ``archived`` filter), same ``{"items": [{"key", …}], "next_cursor"}``
envelope — read off ``public-v1-openapi.json``'s ``PublicFlagListItemPagedResult``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from flag_cleanup.urls import without_userinfo


class FeatureflipApiError(RuntimeError):
    """The Featureflip API refused a request.

    Exists so the failure can be reported without httpx's own message, which is
    built from ``str(request.url)`` — and ``httpx.URL.__str__`` does NOT redact
    a password (only ``__repr__`` does), so letting the raw error escape prints
    any ``api-url`` basic-auth credentials into the workflow log, three times
    over: the log line, the traceback, and stderr.
    """


def _raise_for_status_without_credentials(response: httpx.Response) -> None:
    """``raise_for_status`` with the URL's userinfo stripped from the message.

    Also turns the most likely first-run failure — a wrong or expired
    ``FEATUREFLIP_API_TOKEN`` — into a sentence that says so, rather than a
    bare httpx traceback.
    """
    if not response.is_error:
        return

    safe_url = without_userinfo(str(response.request.url))
    if response.status_code in (401, 403):
        hint = (
            " Check FEATUREFLIP_API_TOKEN is a current token with access to "
            "this organization and project."
        )
    elif response.status_code == 404:
        hint = " Check the org and project slugs."
    else:
        hint = ""
    raise FeatureflipApiError(
        f"Featureflip API returned {response.status_code} for {safe_url}.{hint}"
    )

logger = logging.getLogger(__name__)

_REMOVAL_CANDIDATES_PATH = "/api/v1/orgs/{org}/projects/{project}/flags/removal-candidates"
_FLAGS_PATH = "/api/v1/orgs/{org}/projects/{project}/flags"
_ARCHIVE_PATH = "/api/v1/orgs/{org}/projects/{project}/flags/{flag}/archive"


def _envelope_detail(response: httpx.Response) -> str:
    """The actionable part of a public-API error envelope, as one string.

    The envelope is ``{error, message, docs_url, fields, ...}``, and for the
    two refusals archive-on-merge actually meets, ``message`` alone says
    nothing: a ``ValidationException`` renders as the generic *"One or more
    fields are invalid."* while the reason a flag cannot be archived —
    ``FLAG_HAS_DEPENDENTS: other-flag`` or ``FLAG_HAS_PENDING_SCHEDULES`` —
    is down in ``fields``. Reporting only ``message`` would tell a customer
    their merge did not archive the flag without telling them why or what to
    do, which is the failure this whole tool is shaped against.

    Falls back to the raw body, truncated: a non-JSON response here means a
    proxy or gateway answered instead of the API, and its text is usually the
    only clue about which.
    """
    try:
        payload = response.json()
    except ValueError:
        body = response.text.strip()
        return body[:200] + ("..." if len(body) > 200 else "") if body else ""
    if not isinstance(payload, dict):
        return ""

    parts: list[str] = []
    message = payload.get("message")
    if isinstance(message, str) and message:
        parts.append(message)

    fields = payload.get("fields")
    if isinstance(fields, dict):
        for name, problems in fields.items():
            rendered = (
                "; ".join(str(problem) for problem in problems)
                if isinstance(problems, (list, tuple))
                else str(problems)
            )
            if rendered:
                parts.append(f"{name}: {rendered}")
    return " ".join(parts)


def _require_bool(value: object, key: object) -> bool:
    """``value`` as a real ``bool``, or refuse the whole walk.

    ``treatment`` decides which side of every ``if`` this tool DELETES, and
    Python's truthiness would answer it wrongly and silently: ``bool("false")``
    is ``True``, so a serializer change, a proxy that stringifies, or a future
    contract tweak sending the string ``"false"`` would keep the ON branch,
    delete the live OFF branch, and open a ready-for-review pull request whose
    body states the opposite. ``None`` fails the mirror-image way.

    Refused rather than coerced, because there is no safe reading: unlike
    ``status`` (where "unrecognised" can default to the more cautious draft
    PR), both values of ``treatment`` are destructive if wrong. Refused for
    the whole run rather than per candidate, because the cause is a broken
    contract on OUR side, not one odd flag — every later candidate is
    suspect. ``__main__`` renders this as a clean exit 1 and still reports the
    candidates that completed.
    """
    if isinstance(value, bool):
        return value
    raise FeatureflipApiError(
        f"removal-candidates returned a non-boolean `treatment` ({value!r}) for "
        f"flag {key!r}. That field decides which branch of every flag check is "
        "deleted, so it is refused rather than guessed at"
    )


def _require_str(item: dict, field: str, endpoint: str = "removal-candidates") -> str:
    """``item[field]`` as a real ``str``, or refuse the whole walk.

    The same protection :func:`_require_bool` gives ``treatment``, for the
    three fields that were read with ``item[...]``. A serializer change that
    drops or renames one raised ``KeyError`` from inside the generator —
    a type ``__main__`` does not recognise, so the customer's workflow log got
    a full traceback instead of the one sentence that says the contract moved.

    Type-checked as well as present, because neither field survives another
    type: ``key`` becomes a git branch name and a PR title, ``status`` is
    compared case-insensitively, and ``reason`` is written into the PR body.

    Refused for the whole run rather than per candidate, for
    :func:`_require_bool`'s reason: the cause is a broken contract, not one odd
    flag, so every later candidate is suspect too.
    """
    value = item.get(field)
    if isinstance(value, str):
        return value
    missing = field not in item
    raise FeatureflipApiError(
        f"{endpoint} returned {'no' if missing else 'a non-string'} "
        f"`{field}`{'' if missing else f' ({value!r})'} for flag "
        f"{item.get('key')!r}. The endpoint contract this Action is built "
        "against is frozen, so a response that does not match it is refused "
        "rather than guessed at"
    )

#: Hard cap on pages fetched for ONE ``_pages`` walk — so it bounds
#: :meth:`FeatureflipClient.flag_keys` exactly as it bounds
#: :meth:`FeatureflipClient.removal_candidates`. Neither endpoint returns more
#: than one page per flag-batch and a project with a million flags is not a
#: real shape, so this can only be reached by a server that pages forever.
#: Bounded because the alternative — trusting the cursor — is an unbounded
#: request loop against the CUSTOMER's own API quota.
MAX_PAGES = 1000


@dataclass(frozen=True, slots=True)
class Candidate:
    """One flag the backend considers safe to remove.

    ``status`` is carried through exactly as the API returned it
    (``"Dead"``/``"Stale"``, PascalCase). Callers must compare
    case-insensitively rather than assuming this casing — and must NOT
    normalise/lowercase it here, so a caller that wants the raw wire value
    (e.g. for a PR body) still gets it.
    """

    key: str
    reason: str
    treatment: bool
    status: str


class FeatureflipClient:
    """Thin client for the ``removal-candidates`` endpoint.

    Pass ``transport`` (e.g. ``httpx.MockTransport``) in tests — this class
    never hardcodes a real network call, so it is fully mockable without
    monkeypatching.
    """

    def __init__(
        self,
        api_url: str,
        api_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_token = api_token
        self._client = httpx.Client(base_url=api_url.rstrip("/"), transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FeatureflipClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _pages(self, path: str, params: dict[str, str], endpoint: str) -> Iterator[dict]:
        """Yield each page's payload, walking ``next_cursor`` to exhaustion.

        Termination does NOT rest on the server behaving — see
        :meth:`removal_candidates` for the three cursor failures this guards
        against and why each stops the walk rather than raising. ``endpoint``
        names the route in those warnings.
        """
        headers = {"Authorization": f"Bearer {self._api_token}"}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_PAGES):
            query = dict(params)
            if cursor is not None:
                query["cursor"] = cursor
            response = self._client.get(path, params=query, headers=headers)
            _raise_for_status_without_credentials(response)
            try:
                payload = response.json()
            except ValueError as exc:
                # A 200 whose body is not JSON at all: a proxy, a captive
                # portal or an HTML error page answered instead of the API.
                # `json.JSONDecodeError` is a `ValueError` and would otherwise
                # leave this generator as a type no caller recognises —
                # `orchestrate._fetch_known_flag_keys` catches
                # `FeatureflipApiError`/`httpx.HTTPError` and degrades, and
                # `__main__` renders `FeatureflipApiError` as one sentence, so
                # anything else aborts the whole run with a traceback.
                raise FeatureflipApiError(
                    f"{endpoint} returned a 200 whose body is not JSON "
                    f"({exc}). The endpoint contract this Action is built "
                    "against is frozen, so a response that does not match it "
                    "is refused rather than guessed at"
                ) from exc
            yield payload
            next_cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
            if next_cursor is None:
                return
            if not isinstance(next_cursor, str) or not next_cursor:
                logger.warning(
                    "%s returned a malformed next_cursor (%r); stopping pagination "
                    "rather than looping", endpoint, next_cursor,
                )
                return
            if next_cursor in seen_cursors:
                logger.warning(
                    "%s repeated next_cursor %r; stopping pagination rather than "
                    "looping", endpoint, next_cursor,
                )
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            logger.warning(
                "%s did not terminate within %d pages; stopping there. Some "
                "items may not have been processed.", endpoint, MAX_PAGES,
            )

    def removal_candidates(self, org: str, project: str, staleness: str) -> Iterator[Candidate]:
        """Yield every removal candidate, paginating via ``next_cursor``.

        Termination does NOT rest on the server behaving. A well-formed
        response ends the walk by omitting ``next_cursor`` or sending it as
        ``null``; every other way a cursor can fail to advance is also treated
        as the end, because the failure mode otherwise is an unbounded request
        loop against the customer's API:

        * a falsy or non-``str`` cursor (``""``, ``0``, ``5``, a dict) — the
          contract says ``str | None``, so anything else is malformed and
          cannot safely be sent back as ``?cursor=``;
        * a cursor already seen in this walk — the server is cycling, and
          following it would re-yield the same flags forever;
        * more than :data:`MAX_PAGES` pages — the backstop for a cursor that
          advances every time but never terminates (which the repeat check
          alone cannot catch).

        Each of those stops the walk and logs a warning: the candidates
        collected so far are still returned (removing fewer flags is safe;
        looping is not).
        """
        # Percent-encoded with `safe=''`, matching how `orchestrate._pr_body`
        # builds the same two segments. Nothing in `Config.from_env` rejects a
        # `/`, `#` or `?` in either value, and interpolating one raw silently
        # rewrites the request's path or query rather than 404-ing on the
        # resource that was actually asked for.
        path = _REMOVAL_CANDIDATES_PATH.format(
            org=quote(org, safe=""), project=quote(project, safe="")
        )
        for payload in self._pages(path, {"staleness": staleness}, "removal-candidates"):
            for item in payload.get("items") or []:
                yield Candidate(
                    key=_require_str(item, "key"),
                    reason=_require_str(item, "reason"),
                    treatment=_require_bool(item.get("treatment"), item.get("key")),
                    status=_require_str(item, "status"),
                )

    def flag_keys(self, org: str, project: str) -> frozenset[str]:
        """Every flag key in the project, live AND archived.

        Consumed by the ``*_entries.toml`` registry prong as sibling evidence
        ("another key in this map is a flag of this project"). No ``archived``
        parameter on purpose: the backend's list handler applies that filter
        only when the value is present, so leaving it out returns both, and a
        registry legitimately lists a recently archived key — counting it
        makes the evidence stronger, never weaker.

        A page without ``items`` is refused rather than read as empty: the
        silent alternative is a project that appears to have no flags, which
        turns the registry prong off for every flag with no warning.

        EVERY malformed page shape is refused as :class:`FeatureflipApiError`,
        and that type is the whole point. ``orchestrate._fetch_known_flag_keys``
        catches ``FeatureflipApiError``/``httpx.HTTPError`` and degrades to an
        empty sibling list — the run continues, the registry prong switches
        itself off, the caveat says so. Anything else escapes and ABORTS the
        run, which is a far worse outcome for a page this Action only ever uses
        as optional evidence: a non-JSON body used to raise
        ``json.JSONDecodeError`` (guarded in :meth:`_pages`), ``"items": 5``
        ``TypeError`` from iterating an int, and ``"items": ["x"]``
        ``AttributeError`` from ``_require_str``'s ``item.get``.
        """
        path = _FLAGS_PATH.format(org=quote(org, safe=""), project=quote(project, safe=""))
        keys: set[str] = set()
        for payload in self._pages(path, {}, "flags"):
            if not isinstance(payload, dict) or "items" not in payload:
                raise FeatureflipApiError(
                    "flags returned a page without `items`. The endpoint contract "
                    "this Action is built against is frozen, so a response that "
                    "does not match it is refused rather than guessed at"
                )
            items = payload["items"]
            if items is None:
                continue
            if not isinstance(items, list):
                raise FeatureflipApiError(
                    f"flags returned an `items` that is not a list ({items!r}). "
                    "The endpoint contract this Action is built against is "
                    "frozen, so a response that does not match it is refused "
                    "rather than guessed at"
                )
            for item in items:
                if not isinstance(item, dict):
                    raise FeatureflipApiError(
                        f"flags returned an `items` entry that is not an object "
                        f"({item!r}). The endpoint contract this Action is built "
                        "against is frozen, so a response that does not match it "
                        "is refused rather than guessed at"
                    )
                keys.add(_require_str(item, "key", endpoint="flags"))
        return frozenset(keys)

    def archive_flag(self, org: str, project: str, key: str) -> None:
        """Archive one flag. Returns on success, raises on anything else.

        Idempotent by the backend's own construction: ``FeatureFlag.Archive()``
        returns early when the flag is already archived, so a redelivered
        pull-request event or a re-run of the same job is a second ``204`` and
        not an error. Nothing here needs to track what it has already done.

        The failure cases are NOT interchangeable and each gets its own
        sentence, because the remedies have nothing to do with each other:

        * **404** — the flag is gone, or ``org``/``project`` is wrong. Those
          are indistinguishable from one response and the second is a workflow
          typo that would silently archive nothing forever, so this raises
          rather than shrugging. A flag genuinely deleted between the pull
          request opening and merging is the rarer of the two.
        * **403** — almost always the token. Archiving needs the Member role
          while fetching candidates needs only read, so a workflow that reuses
          its read-only cleanup token lands exactly here, and the message says
          so instead of leaving someone to infer it.
        * **400** — a real refusal by the domain: another live flag lists this
          one as a prerequisite (``FLAG_HAS_DEPENDENTS``), or a scheduled
          change still targets it (``FLAG_HAS_PENDING_SCHEDULES``). The code
          has already merged; the flag simply cannot be archived until that is
          resolved. Surfaced verbatim from ``fields`` — see
          :func:`_envelope_detail`.
        """
        path = _ARCHIVE_PATH.format(
            org=quote(org, safe=""),
            project=quote(project, safe=""),
            # A flag key may legitimately contain `/`, which would otherwise
            # rewrite the request path and archive nothing while reporting a
            # 404 against a resource nobody asked for.
            flag=quote(key, safe=""),
        )
        response = self._client.post(
            path, headers={"Authorization": f"Bearer {self._api_token}"}
        )
        if response.status_code == 204:
            return

        safe_url = without_userinfo(str(response.request.url))
        detail = _envelope_detail(response)
        suffix = f" {detail}" if detail else ""

        if response.status_code == 404:
            raise FeatureflipApiError(
                f"Featureflip API returned 404 archiving flag {key!r}. Either the "
                f"flag no longer exists, or FEATUREFLIP_ORG/FEATUREFLIP_PROJECT "
                f"do not name the project it lives in.{suffix}"
            )
        if response.status_code in (401, 403):
            raise FeatureflipApiError(
                f"Featureflip API returned {response.status_code} archiving flag "
                f"{key!r}. Archiving requires a token with the Member role — a "
                f"read-only token can fetch removal candidates but cannot archive, "
                f"so check this workflow is not passing the same secret the "
                f"cleanup workflow uses.{suffix}"
            )
        if response.status_code == 400:
            raise FeatureflipApiError(
                f"Featureflip refused to archive flag {key!r}: the code was merged "
                f"but the flag cannot be archived yet.{suffix}"
            )
        raise FeatureflipApiError(
            f"Featureflip API returned {response.status_code} for {safe_url}.{suffix}"
        )
