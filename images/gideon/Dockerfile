# GIDEON's service image: the pinned Python interpreter and the
# service's exact dependency set, one role per Compose block (gideon-api now).
# Every version is an images.lock value passed in as a build argument, installed
# without resolution and checked by `pip check`; the only way it is built is
# `python3 -m tools.imagebuild gideon` on the box. The package is bind-mounted
# from the checkout that applied, so this image copies nothing from the
# repository and carries no GIDEON entrypoint. One argument feeds the driver's
# two wheels because the two projects release in lockstep and a half-bump would
# be silent under this writer.
ARG BASE
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
RUN set -eux; \
    pip install --no-deps --only-binary=:all: --no-cache-dir \
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
        "psycopg-binary==${PSYCOPG_VERSION}"; \
    pip check
RUN groupadd --gid 10001 gideon; \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin gideon
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER gideon
