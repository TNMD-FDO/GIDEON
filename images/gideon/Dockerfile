# GIDEON's service image: the pinned Python interpreter and the
# service's exact dependency set, one role per Compose block (gideon-api and
# gideon-worker).
# Every version is an images.lock value passed in as a build argument, installed
# without resolution and checked by `pip check`; the only way it is built is
# `python3 -m tools.imagebuild gideon` on the box. The package is bind-mounted
# from the checkout that applied, so this image copies nothing from the
# repository and carries no GIDEON entrypoint. One argument feeds the driver's
# two wheels because the two projects release in lockstep and a half-bump would
# be silent under this writer. fast-diff-match-patch is built from its pinned
# sdist until upstream publishes a cp314 wheel; all final-stage installs use
# wheels.
ARG BASE
FROM ${BASE} AS wheels
ARG FAST_DIFF_MATCH_PATCH_VERSION
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends g++; \
    rm -rf /var/lib/apt/lists/*; \
    pip wheel --no-deps --no-binary=fast-diff-match-patch \
        --wheel-dir /tmp/wheels \
        "fast-diff-match-patch==${FAST_DIFF_MATCH_PATCH_VERSION}"

FROM ${BASE}
ARG STARLETTE_VERSION
ARG UVICORN_VERSION
ARG HTTPX_VERSION
ARG ANYIO_VERSION
ARG HTTPCORE_VERSION
ARG H11_VERSION
ARG CERTIFI_VERSION
ARG IDNA_VERSION
ARG CLICK_VERSION
ARG TYPING_EXTENSIONS_VERSION
ARG PSYCOPG_VERSION
ARG PROCRASTINATE_VERSION
ARG PSYCOPG_POOL_VERSION
ARG ASGIREF_VERSION
ARG ATTRS_VERSION
ARG CRONITER_VERSION
ARG PYTHON_DATEUTIL_VERSION
ARG SIX_VERSION
ARG PACKAGING_VERSION
ARG EYECITE_VERSION
ARG REPORTERS_DB_VERSION
ARG COURTS_DB_VERSION
ARG PYAHOCORASICK_VERSION
ARG LXML_VERSION
ARG REGEX_VERSION
ARG FAST_DIFF_MATCH_PATCH_VERSION
COPY --from=wheels /tmp/wheels /tmp/wheels
RUN set -eux; \
    pip install --no-deps --only-binary=:all: --no-cache-dir \
        --find-links=/tmp/wheels \
        "starlette==${STARLETTE_VERSION}" \
        "uvicorn==${UVICORN_VERSION}" \
        "httpx==${HTTPX_VERSION}" \
        "anyio==${ANYIO_VERSION}" \
        "httpcore==${HTTPCORE_VERSION}" \
        "h11==${H11_VERSION}" \
        "certifi==${CERTIFI_VERSION}" \
        "idna==${IDNA_VERSION}" \
        "click==${CLICK_VERSION}" \
        "typing_extensions==${TYPING_EXTENSIONS_VERSION}" \
        "psycopg==${PSYCOPG_VERSION}" \
        "psycopg-binary==${PSYCOPG_VERSION}" \
        "procrastinate==${PROCRASTINATE_VERSION}" \
        "psycopg-pool==${PSYCOPG_POOL_VERSION}" \
        "asgiref==${ASGIREF_VERSION}" \
        "attrs==${ATTRS_VERSION}" \
        "croniter==${CRONITER_VERSION}" \
        "python-dateutil==${PYTHON_DATEUTIL_VERSION}" \
        "six==${SIX_VERSION}" \
        "packaging==${PACKAGING_VERSION}" \
        "eyecite==${EYECITE_VERSION}" \
        "reporters-db==${REPORTERS_DB_VERSION}" \
        "courts-db==${COURTS_DB_VERSION}" \
        "pyahocorasick==${PYAHOCORASICK_VERSION}" \
        "lxml==${LXML_VERSION}" \
        "regex==${REGEX_VERSION}" \
        "fast-diff-match-patch==${FAST_DIFF_MATCH_PATCH_VERSION}"; \
    rm -rf /tmp/wheels; \
    pip check
RUN groupadd --gid 10001 gideon; \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin gideon
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER gideon
