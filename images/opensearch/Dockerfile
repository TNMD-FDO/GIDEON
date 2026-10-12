# GIDEON's OpenSearch image: the pinned stock base plus its metrics plugin.
# Every value is an images.lock build argument; the only build is
# `python3 -m tools.imagebuild opensearch` on the box. The plugin must match
# the server, and its release zip has no published checksum, so this file
# compares the lock's digest before installation. The base's user, entrypoint,
# and command are inherited unchanged.
ARG BASE
FROM ${BASE}
ARG PROMETHEUS_EXPORTER_VERSION
ARG PROMETHEUS_EXPORTER_SHA256
RUN set -eux; \
    curl -fSL "https://github.com/opensearch-project/opensearch-prometheus-exporter/releases/download/${PROMETHEUS_EXPORTER_VERSION}/prometheus-exporter-${PROMETHEUS_EXPORTER_VERSION}.zip" -o /tmp/prometheus-exporter.zip; \
    printf '%s  %s\n' "${PROMETHEUS_EXPORTER_SHA256}" /tmp/prometheus-exporter.zip | sha256sum -c -; \
    /usr/share/opensearch/bin/opensearch-plugin install --batch file:///tmp/prometheus-exporter.zip; \
    rm /tmp/prometheus-exporter.zip
