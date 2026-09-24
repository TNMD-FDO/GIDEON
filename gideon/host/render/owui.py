"""Pure Open WebUI environment, frontend posture, and apply-manifest artifacts.

The frontend's one Chat Completions connection is General's service: the base
URL and key belong to ``gideon-api``, and forwarded user-info headers carry the
seat's identity to it, so frontend requests reach the service rather than the
engine.
"""

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from string import Template
from typing import Any, Final
from urllib.parse import quote

import yaml  # type: ignore[import-untyped]

from gideon.host.images import NO_PROXY_LOCAL
from gideon.host.ldap import bind_identity, filter_value
from gideon.host.models import ModelPin
from gideon.host.render import Artifact, RenderInputs, template_text
from gideon.host.render.api import API_SECRET_NAME, API_SERVICE_NAME, api_base_url
from gideon.host.render.proxy import PROXY_AUTH_NAME, proxy_environment_values
from gideon.host.render.searxng import (
    SEARXNG_SERVICE_NAME,
    search_enabled,
    searxng_query_url,
)
from gideon.host.render.yamlout import dump
from gideon.host.site import group_name

PERMISSIONS_TEMPLATE: Final = "open-webui/permissions.yaml"
# General's three texts (name, description, system prompt) are release text in
# one template beside the permission set; the office supplies its name and
# nothing else.
GENERAL_TEMPLATE: Final = "open-webui/general.yaml"
# The branch gate's source (docs/frontend-contract.md §2 row 8): an
# inlet-only global Function the frontend runs and the tests import by path,
# rendered verbatim as a manifest Function on every host, refusing a user-role
# request on a model row that is not a preset.
BRANCH_GATE_TEMPLATE: Final = "open-webui/functions/branch_gate.py"
# Effectively permanent: chat rows, URLs, and the default-model value name it.
# Prefixed like other GIDEON-owned ids, so every GIDEON-owned id starts the
# same way.
GENERAL_PRESET_ID: Final = "gideon-general"
# Effectively permanent: the frontend contract names it; the hyphen-safe
# prefix follows the Function id rule. The frontend sorts Filters by priority
# and id; without Valves this one gets priority 0. Since cutover, it is the
# only global inlet the frontend runs.
BRANCH_GATE_ID: Final = "gideon-branch-gate"
BRANCH_GATE_NAME: Final = "GIDEON branch gate"
# Release text shown on the frontend's Functions page beside the record.
BRANCH_GATE_DESCRIPTION: Final = (
    "The branch gate refuses a user's request to any model that is not one of "
    "GIDEON's branches, such as the hidden base model, and sends the user to General. Global on "
    "every model; pushed by gideon apply, which reverts any edit made here."
)
# General carries no Filter since the cutover: the citation stamp is the
# service's.  The key is kept and rendered empty rather than dropped, so the
# push overwrites a live attachment whether the sync route merges `meta` or
# replaces it, and the read-back's stale-key check still has
# a key to compare.
GENERAL_FILTER_IDS: Final[tuple[str, ...]] = ()
# The feedback adapter reads only the admin list route, which answers each
# rating without its chat snapshot. The frontend admits a key's request when
# the path equals an entry or begins with it and a slash; the admin role gets
# no bypass. This exact entry admits the one GET and leaves the export twin,
# deletes, and arena configuration under the evaluations router out.
FEEDBACK_LIST_ROUTE: Final[str] = "/api/v1/evaluations/feedbacks/list"
ALLOWED_ENDPOINTS: Final[tuple[str, ...]] = (
    # Prefixes, not just the sync paths: the admin key lists Functions and
    # models before each desired-state sync so removals are reported by id;
    # role gating still refuses the eval identity every admin-only listing.
    # The feedback list route alone is an exact path, not a prefix.
    "/api/v1/functions",
    "/api/v1/models",
    "/api/v1/groups",
    "/api/v1/users",
    "/api/v1/knowledge",
    "/api/v1/auths/add",
    "/api/chat/completions",
    "/api/models",
    FEEDBACK_LIST_ROUTE,
)
SERVICE_GROUP: Final = "gideon-service"


@dataclass(frozen=True, slots=True)
class MachineIdentity:
    """One of the exactly two non-human accounts."""

    username: str
    email: str
    role: str
    groups: tuple[str, ...] = ()


# The .invalid TLD (RFC 2606) keeps both addresses hostname-independent and
# unroutable; the frontend requires an email per account.
BREAK_GLASS: Final = MachineIdentity("gideon-admin", "gideon-admin@gideon.invalid", "admin")
EVAL_IDENTITY: Final = MachineIdentity(
    "gideon-eval", "gideon-eval@gideon.invalid", "user", (SERVICE_GROUP,)
)
EVAL_PASSWORD_SECRET: Final[str] = "gideon_eval_password"
MACHINE_IDENTITIES: Final[tuple[MachineIdentity, ...]] = (BREAK_GLASS, EVAL_IDENTITY)
OWUI_SECRET_NAMES: Final[tuple[str, ...]] = (
    "ldap_bind_password",
    "postgres_openwebui_password",
    "gideon_admin_password",
    API_SECRET_NAME,
)
# The pinned frontend shows this once per chat while the search toggle stays
# on.
WEB_SEARCH_CONFIRMATION_TEXT: Final[str] = (
    "Turning on search sends what you type — as search queries — to internet search engines, "
    "and fetches pages from the web. Do not paste client or case material here."
)
# The citations component's sources toggle and both rows of web-search results
# load an image from a third-party favicon service, using the source link as
# its query. The toggle falls back to the frontend's logo when it fails; the
# rows have no fallback. The pinned backend's security-headers module reads this
# value from the environment and its pure-ASGI middleware sets it on every
# response, computed once at start.  ``'self'`` admits the frontend's own files
# and uploads, ``data:`` admits stored profile images and forms-plugin inline
# backgrounds, and ``blob:`` admits the composer's previews.  No ``default-src``
# is set, so every other directive is a later ruling; the ingress header is the
# fallback if a frontend bump drops this setting. exempt: policy.
CONTENT_SECURITY_POLICY: Final[str] = "img-src 'self' data: blob:"
# The pinned frontend (Open WebUI v0.11.3) reads ``USER_AGENT`` in its
# environment module, and ``safe_web`` puts it on the session headers every
# fetch forwards. Its own comment names Wikipedia and Cloudflare as blockers
# of langchain's default placeholder. When unset, that placeholder goes out;
# Wikipedia answers it with a 403 whose body is stored as the page because
# non-2xx responses do not raise. This descriptive product name and repository
# address, the convention a crawler declares itself by, turns Wikipedia's
# refusal into the page and changes nothing else in the 23-site probe.
# The value has no version, keeping the frontend's Compose block digest stable;
# a browser-shaped agent was declined.
# The SearXNG client's own request carries its named bot agent, unrelated to
# this value. exempt: policy.
WEB_LOADER_USER_AGENT: Final[str] = "GIDEON (+https://github.com/TNMD-FDO/gideon)"
# The loader's per-page bound in seconds. The pinned loader's session sets no
# timeout unless the loader factory is given one (``WEB_LOADER_TIMEOUT``,
# passed through as each fetch's total), so aiohttp's own 300 s default bounds
# a page that never answers; one site tarpits any address-carrying agent, and
# the factory's continue_on_failure then skips the page as an empty document
# once the bound passes while the other pages load.  Thirty seconds against
# pages that answer in a fraction of a second: a starting value corrected by
# watching the box.  exempt: policy.
WEB_LOADER_TIMEOUT_SECONDS: Final[int] = 30
# Three results per query, a fixed policy value recorded as exempt. The pinned
# client enforces it per query in code, while the query count comes from the
# task model.
WEB_SEARCH_RESULT_COUNT: Final[int] = 3
# The frontend's audit sink defaults to a file under its data directory. An
# explicit path gives the search-path check one known source.
AUDIT_LOG_FILE: Final[str] = "/app/backend/data/audit.log"
# The pinned frontend's default excluded paths, rendered as its comma-separated
# environment form.
AUDIT_EXCLUDED_PATHS: Final[str] = "/chats,/chat,/folders"
_WORKSPACE_ACCESS_FLAGS: Final[frozenset[str]] = frozenset(
    {"models", "knowledge", "prompts", "tools", "skills"}
)

# The base model's own record disables built-in tools because the frontend
# attaches them to every UI turn unless the model's record says no, and the
# engine has no tool-call parser. A bare base model has no rig for any other
# capability either: all are off, including `terminal`, which also attaches
# tools. Display-only capabilities (citations, status_updates, usage) are
# deliberately absent, so the frontend keeps its own defaults. Every key is
# the pinned frontend's name.
BASE_MODEL_CAPABILITIES: Final[Mapping[str, bool]] = {
    "builtin_tools": False,
    "file_upload": False,
    "file_context": False,
    "vision": False,
    "memory": False,
    "code_interpreter": False,
    "image_generation": False,
    "web_search": False,
    "terminal": False,
}

# General's capabilities: web search and file context on, the other seven off.
# The pinned middleware runs its files handler only under the record's
# `file_context`; the search handler's loaded pages are
# files, and that handler is the only step that applies the citation template
# to them, so this flag makes the loaded pages available whole in context.
# `file_upload: false` keeps attachments out and `builtin_tools: false` keeps
# file tools out; the base record is unchanged.
# A preset inherits its connection from the base but no capability, so it needs
# its own set. With no `defaultFeatureIds`, an enabled capability is offered
# but off for every new chat, and `memory: false` on the record forces memory
# off for every user regardless of the frontend's defaults or personal
# settings. `terminal` stays off beside `builtin_tools`.
GENERAL_CAPABILITIES: Final[Mapping[str, bool]] = {
    **BASE_MODEL_CAPABILITIES,
    "web_search": True,
    "file_context": True,
}

# No suggestion cards on General's new-chat screen (exempt: policy toggles).
# The record is the one lever: at boot the pinned frontend replaces an empty
# DEFAULT_PROMPT_SUGGESTIONS with its stock six. The screen uses that list only
# when the record has none of its own, and otherwise shows the name and
# description alone. A product list is later fog, to be written after real use
# has been watched under general.yaml's rules.
GENERAL_SUGGESTION_PROMPTS: Final[tuple[Mapping[str, str], ...]] = ()

# This is the request shape the engine can serve, not a sampling override.
# The per-chat search toggle runs the non-tool path, using queries from the
# task model and loading whole pages into context, only under this mode. Under
# the default `native`, it expects the `search_web` built-in tool, which
# `builtin_tools: false` suppresses, so the toggle silently does nothing. The
# mode also stops built-in tools being added at all and opens nothing else:
# other gated paths sit behind capabilities that are off.
GENERAL_FUNCTION_CALLING: Final[str] = "legacy"

# The pinned frontend enforces read access on every hop of a preset's base
# chain at generation time — a base model with no row is admin-only, one with
# a row needs a grant the caller passes — so a base the users cannot read
# makes every preset over it answer "Model not found" for a `user`-role
# account. The base model's record therefore carries the public-read grant.
# The client-side `meta.hidden` rule keeps it out of the selector and default
# selection, but the API still lists it to a signed-in user, who cannot mint a
# key.
BASE_MODEL_HIDDEN: Final[bool] = True

# Both model and Function sync forms carry these row fields. The server
# replaces the owner with the syncing admin's id and the update time with its
# clock, while it stores the creation time verbatim. Shared placeholders keep
# both deterministic.
SYNC_ROW_USER_ID: Final[str] = BREAK_GLASS.username
SYNC_ROW_CREATED_AT: Final[int] = 0
SYNC_ROW_UPDATED_AT: Final[int] = 0

# A grant entry in the sync payload must carry an id, a resource type, a
# resource id, and a creation time the server discards, keeping only the
# (principal_type, principal_id, permission) triple. The server forces the
# resource fields; the id and time are deterministic placeholders.
MODEL_GRANT_ID: Final[str] = "gideon-public-read"
MODEL_GRANT_RESOURCE_TYPE: Final[str] = "model"
MODEL_GRANT_CREATED_AT: Final[int] = 0


@dataclass(frozen=True, slots=True)
class GeneralTexts:
    """The three release texts used by General's preset record."""

    name: str
    description: str
    system_prompt: str


def permission_tree(inputs: RenderInputs) -> dict[str, dict[str, bool]]:
    """Load the complete versioned permission tree supplied to render."""

    try:
        source = inputs.templates[PERMISSIONS_TEMPLATE]
    except KeyError as exc:
        raise ValueError(f"Render template {PERMISSIONS_TEMPLATE} is missing.") from exc
    try:
        document = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise ValueError(f"Render template {PERMISSIONS_TEMPLATE} is invalid: {exc}") from exc
    if not isinstance(document, Mapping):
        raise TypeError(f"Render template {PERMISSIONS_TEMPLATE} must be a mapping.")
    tree: dict[str, dict[str, bool]] = {}
    for section, values in document.items():
        if not isinstance(section, str) or not isinstance(values, Mapping):
            raise TypeError(f"Render template {PERMISSIONS_TEMPLATE} has an invalid section.")
        leaves: dict[str, bool] = {}
        for leaf, value in values.items():
            if not isinstance(leaf, str) or not isinstance(value, bool):
                raise TypeError(
                    f"Render template {PERMISSIONS_TEMPLATE} has an invalid permission leaf."
                )
            leaves[leaf] = value
        tree[section] = leaves
    return tree


_GENERAL_TEXT_FIELDS: Final[tuple[str, ...]] = ("name", "description", "system_prompt")


def _general_error(leaf: str, detail: str) -> ValueError:
    return ValueError(f"Render template {GENERAL_TEMPLATE} leaf {leaf} {detail}.")


def general_texts(inputs: RenderInputs) -> GeneralTexts:
    """Load General's three release texts with the office's name filled in.

    The document is parsed first and each leaf substituted afterwards, so an
    office name carrying YAML punctuation cannot break the document.  The one
    placeholder is ``$office_name`` and a literal dollar sign is written
    ``$$`` (``string.Template``'s own rules); the frontend's ``{{VARIABLE}}``
    substitution is never used in these texts; the tests hold the template to
    this rule because the frontend applies its own variable substitution.
    """

    try:
        source = inputs.templates[GENERAL_TEMPLATE]
    except KeyError as exc:
        raise ValueError(f"Render template {GENERAL_TEMPLATE} is missing.") from exc
    try:
        document = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise ValueError(f"Render template {GENERAL_TEMPLATE} is invalid: {exc}") from exc
    if not isinstance(document, Mapping):
        raise TypeError(f"Render template {GENERAL_TEMPLATE} must be a mapping.")
    for leaf in _GENERAL_TEXT_FIELDS:
        if leaf not in document:
            raise _general_error(leaf, "is missing")
    for key in document:
        if key not in _GENERAL_TEXT_FIELDS:
            raise _general_error(str(key), "is not one of name, description, system_prompt")
    values: dict[str, str] = {}
    for leaf in _GENERAL_TEXT_FIELDS:
        value = document[leaf]
        if not isinstance(value, str) or not value:
            raise _general_error(leaf, "must be a non-empty string")
        try:
            values[leaf] = Template(value).substitute(office_name=inputs.site.office.name)
        except (KeyError, ValueError) as exc:
            raise _general_error(
                leaf,
                "may carry only the $office_name placeholder; write a literal dollar sign as $$",
            ) from exc
    return GeneralTexts(**values)


def _permission_environment(tree: Mapping[str, Mapping[str, bool]]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for section, leaves in tree.items():
        for leaf, value in leaves.items():
            suffix = "_ACCESS" if section == "workspace" and leaf in _WORKSPACE_ACCESS_FLAGS else ""
            name = f"USER_PERMISSIONS_{section}_{leaf}{suffix}".upper()
            environment[name] = "true" if value else "false"
    return environment


def _generator_model(inputs: RenderInputs) -> ModelPin:
    generator = inputs.profile.model("generator")
    if generator is None:
        raise ValueError(
            f"Cannot render Compose: profile {inputs.profile.name} has no generator model. "
            "Add the generator model to models.lock, then re-run render."
        )
    return generator


def owui_environment(
    inputs: RenderInputs,
    *,
    engine: bool = True,
    search: bool = True,
    directory: bool = True,
) -> Mapping[str, str]:
    """Return the non-secret Compose environment for Open WebUI.

    The service connection is rendered only on a GPU host and only when
    ``engine`` is true — the drill passes false because its project has no
    service to reach. ``search`` and ``directory`` false render the search
    switch and ``ENABLE_LDAP`` off with no search or LDAP keys whatever the
    site says — the ``gideon-ci`` sibling, which has neither.
    """

    ldap = inputs.site.auth.ldap
    connected = engine and not inputs.no_gpu
    searching = engine and search and search_enabled(inputs)
    environment: dict[str, str] = {
        "WEBUI_NAME": "GIDEON",
        "WEBUI_URL": f"https://{inputs.site.hostname}",
        "ENABLE_PERSISTENT_CONFIG": "false",
        "ENABLE_SIGNUP": "false",
        "ENABLE_LOGIN_FORM": "true",
        "DEFAULT_USER_ROLE": "user",
        "WEBUI_ADMIN_EMAIL": BREAK_GLASS.email,
        "WEBUI_ADMIN_NAME": BREAK_GLASS.username,
        "WEBUI_SECRET_KEY_FILE": "/run/secrets/webui_secret_key",
        "JWT_EXPIRES_IN": f"{inputs.site.auth.session_hours}h",
        "WEBUI_SESSION_COOKIE_SECURE": "true",
        "WEBUI_SESSION_COOKIE_SAME_SITE": "strict",
        # The frontend's process-wide logger threshold controls search-query
        # diagnostics; the audit level controls content detail, not Python log
        # severity.
        "GLOBAL_LOG_LEVEL": "INFO",
        "AUDIT_LOG_LEVEL": "METADATA",
        # The audit default is a file below DATA_DIR, and stdout is disabled so
        # audit rows do not reach the container journal.
        "AUDIT_LOGS_FILE_PATH": AUDIT_LOG_FILE,
        "ENABLE_AUDIT_STDOUT": "false",
        "AUDIT_EXCLUDED_PATHS": AUDIT_EXCLUDED_PATHS,
        # The pinned middleware audits POST, PUT, PATCH, and DELETE; enabling
        # this switch adds GET, putting a page load's URI and query string into
        # the audit file.  Off by default, and rendered by name as
        # ENABLE_QUERIES_CACHE is.
        "ENABLE_AUDIT_GET_REQUESTS": "false",
        # The frontend clears uvicorn's two loggers at startup and attaches its
        # own handler to each logger named here. The default uvicorn.access
        # logger would restore access lines to the journal after the Compose
        # command disables them; each connection asks for handlers when it
        # opens. Naming uvicorn.error keeps the flag effective and sends
        # harmless lifecycle lines to the journal. The value is comma-split
        # without trimming.
        "AUDIT_UVICORN_LOGGER_NAMES": "uvicorn.error",
        # OpenTelemetry's root and signal gates are explicit: tracing needs both
        # root and trace gates, while metrics and logs have their own gates.
        "ENABLE_OTEL": "false",
        "ENABLE_OTEL_TRACES": "false",
        "ENABLE_OTEL_METRICS": "false",
        "ENABLE_OTEL_LOGS": "false",
        # The same-request query cache emits generated queries at INFO, so it
        # stays off at its pinned default.
        "ENABLE_QUERIES_CACHE": "false",
        # Loguru's diagnostic dump can include local values in tracebacks; it
        # is pinned off.
        "LOGURU_DIAGNOSE": "false",
        "ENABLE_ADMIN_CHAT_ACCESS": "true",
        "BYPASS_ADMIN_ACCESS_CONTROL": "true",
        "ENABLE_API_KEYS": "true",
        "ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS": "true",
        "API_KEYS_ALLOWED_ENDPOINTS": ",".join(ALLOWED_ENDPOINTS),
        "BYPASS_EMBEDDING_AND_RETRIEVAL": "true",
        # This governs the search path and the attach route's HTML branch;
        # General uses neither vectors nor embeddings.
        "BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL": "true",
        "STORAGE_PROVIDER": "local",
        "ENABLE_OLLAMA_API": "false",
        "ENABLE_OPENAI_API": "false",
    }
    if connected:
        generator = _generator_model(inputs)
        # One Chat Completions connection, discovered from the service's model
        # list. Leaving out OPENAI_API_CONFIGS keeps the request shape Chat
        # Completions and uses live model discovery. The generator remains the
        # task model, with follow-ups and autocomplete off. The key rides the
        # env file.
        environment.update(
            {
                "ENABLE_OPENAI_API": "true",
                "OPENAI_API_BASE_URLS": api_base_url(),
                # Forward plain user-info headers to the service; the signed-JWT
                # form is unused, and the service reads the email header as its
                # source identity.
                "ENABLE_FORWARD_USER_INFO_HEADERS": "true",
                "TASK_MODEL_EXTERNAL": generator.serve.served_name,
                "ENABLE_TITLE_GENERATION": "true",
                "ENABLE_TAGS_GENERATION": "true",
                "ENABLE_SEARCH_QUERY_GENERATION": "true",
                # The files handler asks the task model for retrieval queries
                # unless every files item is marked full-context; the search
                # handler's bypass append marks nothing, and the same-request
                # cache is off so no query text reaches a log.  Under bypass the
                # queries are never read, so the call is pure cost.  Disabled,
                # the task router raises the feature-disabled exception inside
                # the handler's own try, which passes, and the sources are
                # built from the docs whole.  The upstream default is true; the
                # later Research branch renders its own value.
                "ENABLE_RETRIEVAL_QUERY_GENERATION": "false",
                "ENABLE_FOLLOW_UP_GENERATION": "false",
                "ENABLE_AUTOCOMPLETE_GENERATION": "false",
                # General is the frontend's default model for every user.  The
                # pinned frontend reads this as one raw comma-separated id list,
                # serves it as `default_models` to a signed-in caller, and a new
                # chat takes it after a `?model=` parameter, a folder's pins,
                # and the user's own default, and before its fallback to the
                # first visible model; every path but the parameter skips a
                # hidden model.  Rendered where General's record is (a connected
                # GPU host).  A panel edit lives in the process's memory until
                # the frontend next starts, which apply causes only when a
                # frontend-owned file changed.
                "DEFAULT_MODELS": GENERAL_PRESET_ID,
            }
        )
    if searching:
        environment.update(
            {
                # Use unmodified SearXNG for the non-tool path General's record
                # opens.
                "ENABLE_WEB_SEARCH": "true",
                "WEB_SEARCH_ENGINE": "searxng",
                # The frontend client builds its own query string.
                "SEARXNG_QUERY_URL": searxng_query_url(),
                # Return three results per query.
                "WEB_SEARCH_RESULT_COUNT": str(WEB_SEARCH_RESULT_COUNT),
                # The safe_web loader fetches whole pages; the snippet shortcut
                # stays off.
                "WEB_LOADER_ENGINE": "safe_web",
                "USER_AGENT": WEB_LOADER_USER_AGENT,
                "WEB_LOADER_TIMEOUT": str(WEB_LOADER_TIMEOUT_SECONDS),
                "BYPASS_WEB_SEARCH_WEB_LOADER": "false",
                # The loader honors the proxy environment variables.
                "WEB_SEARCH_TRUST_ENV": "true",
                "ENABLE_WEB_LOADER_SSL_VERIFICATION": "true",
                # Let the frontend show its search reminder.
                "ENABLE_WEB_SEARCH_CONFIRMATION": "true",
                "WEB_SEARCH_CONFIRMATION_CONTENT": WEB_SEARCH_CONFIRMATION_TEXT,
                # The pinned frontend parses this setting as a JSON array.
                "WEB_SEARCH_DOMAIN_FILTER_LIST": json.dumps(
                    list(inputs.site.web.domain_filter), separators=(",", ":")
                ),
            }
        )
    else:
        # Disconnected frontends and sites with search off have no search path.
        environment["ENABLE_WEB_SEARCH"] = "false"
    environment.update(
        {
            # Thumbs on for every chat the frontend shows: the pinned frontend
            # shows them when this switch and the chat.rate_response permission
            # both hold and reads no model record for either, so there is no
            # per-model gate.  Upstream default true; exempt: policy toggle.
            "ENABLE_MESSAGE_RATING": "true",
            # The pinned frontend's evaluation arena (anonymous chatbots,
            # votes, a leaderboard) is on by default and offered to admins
            # alone; an arena turn draws a model at random from the
            # process-wide cache with no access or hidden check, so on this
            # box it could answer from the bare base model with no system
            # prompt.  A feature outside the evaluation harness and General's
            # feature list, off for every role on every host (exempt: policy
            # toggles).  The Admin → Evaluations page stays.
            "ENABLE_EVALUATION_ARENA_MODELS": "false",
            # No egress off the allowlist and no self-update nudge: releases are
            # digest-pinned, so the frontend never phones home for versions.
            "ENABLE_VERSION_UPDATE_CHECK": "false",
            "CONTENT_SECURITY_POLICY": CONTENT_SECURITY_POLICY,
            "TZ": inputs.site.office.timezone,
            "ENABLE_LDAP": "true" if directory else "false",
        }
    )
    if directory:
        environment.update(
            {
                "LDAP_SERVER_LABEL": ldap.host,
                "LDAP_SERVER_HOST": ldap.host,
                "LDAP_SERVER_PORT": str(ldap.port),
                "LDAP_USE_TLS": "true",
                "LDAP_VALIDATE_CERT": "true",
                "LDAP_CA_CERT_FILE": "/etc/gideon/ca.pem",
                "LDAP_APP_DN": bind_identity(ldap.bind_user, ldap.host),
                "LDAP_SEARCH_BASE": ldap.search_base,
                "LDAP_SEARCH_FILTERS": f"(memberOf={filter_value(ldap.users_group_dn)})",
                "LDAP_ATTRIBUTE_FOR_USERNAME": "sAMAccountName",
                # The frontend keys every human by this address, which is an
                # identity rather than a mailbox; every directory account has one.
                "LDAP_ATTRIBUTE_FOR_MAIL": "userPrincipalName",
                "ENABLE_LDAP_GROUP_MANAGEMENT": "true",
                "ENABLE_LDAP_GROUP_CREATION": "false",
                "LDAP_ATTRIBUTE_FOR_GROUPS": "memberOf",
            }
        )
    environment.update(_permission_environment(permission_tree(inputs)))
    if inputs.site.egress_proxy:
        # The frontend's HTTP client reads the proxy variables unconditionally,
        # so the service's name keeps the connection on the Compose network.
        no_proxy = f"{NO_PROXY_LOCAL},caddy,postgres,open-webui"
        if connected:
            no_proxy += f",{API_SERVICE_NAME}"
        if searching:
            no_proxy += f",{SEARXNG_SERVICE_NAME}"
        environment["NO_PROXY"] = no_proxy
    return environment


def base_model_record(inputs: RenderInputs) -> Mapping[str, object]:
    """Build the GPU host's desired record for the discovered base model.

    Readable by every verified user (the base hop of General's access check)
    and hidden from the selector — see ``BASE_MODEL_HIDDEN``.
    """

    served_name = _generator_model(inputs).serve.served_name
    return {
        "id": served_name,
        "user_id": SYNC_ROW_USER_ID,
        "base_model_id": None,
        "name": served_name,
        "params": {},
        "meta": {"hidden": BASE_MODEL_HIDDEN, "capabilities": dict(BASE_MODEL_CAPABILITIES)},
        "access_grants": [dict(public_read_grant(served_name))],
        "is_active": True,
        "updated_at": SYNC_ROW_UPDATED_AT,
        "created_at": SYNC_ROW_CREATED_AT,
    }


def public_read_grant(model_id: str) -> Mapping[str, object]:
    """Build the frontend's public-to-verified-users grant for one model.

    The principal, principal id, and permission triple gives authenticated
    users visibility. The sync form also requires an id, resource type, resource
    id, and creation time, which the server discards.
    """

    return {
        "id": MODEL_GRANT_ID,
        "resource_type": MODEL_GRANT_RESOURCE_TYPE,
        "resource_id": model_id,
        "principal_type": "user",
        "principal_id": "*",
        "permission": "read",
        "created_at": MODEL_GRANT_CREATED_AT,
    }


def general_preset_record(inputs: RenderInputs) -> Mapping[str, object]:
    """Build General's desired preset row over the selected generator."""

    generator = _generator_model(inputs)
    texts = general_texts(inputs)
    # The frontend reads a preset's params from its own record and stores its
    # grants as principal, principal id, and permission triples.
    return {
        "id": GENERAL_PRESET_ID,
        "user_id": SYNC_ROW_USER_ID,
        "base_model_id": generator.serve.served_name,
        "name": texts.name,
        "params": {
            "system": texts.system_prompt,
            "function_calling": GENERAL_FUNCTION_CALLING,
        },
        "meta": {
            "description": texts.description,
            "capabilities": dict(GENERAL_CAPABILITIES),
            "suggestion_prompts": [dict(prompt) for prompt in GENERAL_SUGGESTION_PROMPTS],
            "filterIds": list(GENERAL_FILTER_IDS),
        },
        "access_grants": [dict(public_read_grant(GENERAL_PRESET_ID))],
        "is_active": True,
        "updated_at": SYNC_ROW_UPDATED_AT,
        "created_at": SYNC_ROW_CREATED_AT,
    }


def owui_secret_names(
    inputs: RenderInputs, *, directory: bool = True
) -> tuple[str, ...]:
    """The secret names the frontend's env file reads on this host, in emit order.

    The one declaration the emitter and the consumer map share: the LDAP bind
    password, the frontend's database password, the break-glass password, the
    service's key while the marker is absent, and the proxy credential when the
    inputs carry it.
    """

    names = ["postgres_openwebui_password", "gideon_admin_password"]
    if directory:
        names.insert(0, "ldap_bind_password")
    if not inputs.no_gpu:
        names.append(API_SECRET_NAME)
    # The proxy helper reads the credential only under a configured proxy, so a
    # leftover credential file with the proxy off is carried by nothing.
    if inputs.site.egress_proxy and PROXY_AUTH_NAME in inputs.secrets:
        names.append(PROXY_AUTH_NAME)
    return tuple(names)


def owui_secret_environment(
    inputs: RenderInputs, *, directory: bool = True
) -> Mapping[str, str]:
    """Return raw env-file values, including the service key for GPU hosts."""

    def required(name: str) -> str:
        value = inputs.secrets.get(name)
        if value is None:
            raise ValueError(f"Render secret is missing: {name}.")
        return value

    names = owui_secret_names(inputs, directory=directory)
    values: dict[str, str] = {
        "DATABASE_URL": (
            "postgresql://openwebui:"
            + quote(required("postgres_openwebui_password"), safe="")
            + "@postgres:5432/openwebui"
        ),
        "WEBUI_ADMIN_PASSWORD": required("gideon_admin_password"),
    }
    if directory:
        values = {"LDAP_APP_PASSWORD": required("ldap_bind_password"), **values}
    if API_SECRET_NAME in names:
        values["OPENAI_API_KEYS"] = required(API_SECRET_NAME)
    values.update(proxy_environment_values(inputs))
    return values


def owui_env_file_text(values: Mapping[str, str]) -> str:
    """Render Open WebUI's secret environment mapping as raw lines."""

    return "".join(f"{name}={value}\n" for name, value in values.items())


class OwuiEnvArtifact(Artifact):
    """Render the raw, secret-bearing Open WebUI env file.

    The frontend reads passwords and API keys from its environment only, so
    this file carries the LDAP bind password, database URL, break-glass
    password, and — on a GPU host — the service's key, which the frontend pairs
    with its one base URL.
    """

    name = "open-webui-env"
    relative_path = "open-webui/env"
    mode = 0o600
    owners = ("open-webui",)
    secret = True

    def secret_names(self, inputs: RenderInputs) -> tuple[str, ...]:
        return owui_secret_names(inputs)

    def emit(self, inputs: RenderInputs) -> str:
        return owui_env_file_text(owui_secret_environment(inputs))


def _manifest_permissions(inputs: RenderInputs, *, api_keys: bool = False) -> dict[str, dict[str, bool]]:
    permissions = deepcopy(permission_tree(inputs))
    if api_keys:
        permissions["features"]["api_keys"] = True
    return permissions


def _manifest_group(name: str, membership: str, permissions: Mapping[str, Mapping[str, bool]]) -> Mapping[str, object]:
    return {
        "name": name,
        "membership": membership,
        "permissions": permissions,
    }


def _function_row(
    inputs: RenderInputs,
    template: str,
    identifier: str,
    name: str,
    description: str,
    is_global: bool,
) -> Mapping[str, object]:
    """Build one Filter's row in the pinned Functions sync form.

    The sync execs the content and stores `type`, `is_active`, and `is_global`
    from the payload — both flags default to false when omitted — while it
    overwrites the owner and the update time and stores the creation time
    verbatim. A falsy `valves` is stored as none and read back as an empty
    mapping, which is the row's state for a Filter that defines no Valves. The
    content is compiled first so a broken edit refuses at render, before sync.
    """

    content = template_text(inputs, template)
    try:
        compile(content, template, "exec")
    except SyntaxError as exc:
        line = exc.lineno if exc.lineno is not None else "unknown"
        raise ValueError(
            f"Render template {template} has a syntax error on line {line}: {exc.msg}."
        ) from exc
    return {
        "id": identifier,
        "user_id": SYNC_ROW_USER_ID,
        "name": name,
        "type": "filter",
        "content": content,
        "meta": {"description": description},
        "valves": {},
        "is_active": True,
        "is_global": is_global,
        "updated_at": SYNC_ROW_UPDATED_AT,
        "created_at": SYNC_ROW_CREATED_AT,
    }


def branch_gate_function(inputs: RenderInputs) -> Mapping[str, object]:
    """Build the global branch gate row."""

    return _function_row(
        inputs,
        BRANCH_GATE_TEMPLATE,
        BRANCH_GATE_ID,
        BRANCH_GATE_NAME,
        BRANCH_GATE_DESCRIPTION,
        True,
    )


class ApplyManifestArtifact(Artifact):
    """Render the desired Open WebUI groups, identities, and sync sets.

    The model set holds the base model's own record and General's preset only
    on a GPU host. Both follow the service's presence, which the no-GPU marker
    controls. The branch gate rides every host.
    """

    name = "open-webui-manifest"
    relative_path = "open-webui/manifest.yaml"
    template_paths = (
        PERMISSIONS_TEMPLATE,
        GENERAL_TEMPLATE,
        BRANCH_GATE_TEMPLATE,
    )

    def emit(self, inputs: RenderInputs) -> str:
        ldap = inputs.site.auth.ldap
        permissions = _manifest_permissions(inputs)
        groups: list[Mapping[str, object]] = [
            _manifest_group(group_name(ldap.users_group), "ldap", permissions),
            _manifest_group(group_name(ldap.admins_group), "ldap", permissions),
        ]
        groups.extend(
            _manifest_group(group_name(group), "ldap", permissions)
            for group in ldap.mirror_groups
        )
        groups.append(
            {
                "name": SERVICE_GROUP,
                "membership": "manifest",
                "members": [EVAL_IDENTITY.username],
                "permissions": _manifest_permissions(inputs, api_keys=True),
            }
        )
        identities: list[dict[str, object]] = []
        for machine in MACHINE_IDENTITIES:
            identity: dict[str, object] = {
                "username": machine.username,
                "email": machine.email,
                "role": machine.role,
            }
            if machine.groups:
                identity["groups"] = list(machine.groups)
            identities.append(identity)
        document: Mapping[str, Any] = {
            "groups": groups,
            "identities": identities,
            "functions": [branch_gate_function(inputs)],
            "models": (
                []
                if inputs.no_gpu
                else [base_model_record(inputs), general_preset_record(inputs)]
            ),
        }
        return dump(document)
