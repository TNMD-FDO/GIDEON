"""Derived-store replies for in-process backup and restore command fakes."""

import json
import os
import subprocess
from collections.abc import Mapping, Sequence

from gideon.host import secrets, stack
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_REPOSITORY,
    OPENSEARCH_SERVICE_NAME,
)
from gideon.host.render.qdrant import (
    QDRANT_SECRET_NAME,
    QDRANT_SERVICE_NAME,
    QDRANT_SNAPSHOTS_ROOT,
)


def empty_store_secrets() -> Mapping[str, str]:
    """The visibly fictitious credentials read by a full-run fake."""

    return {
        os.fspath(secrets.secret_path(QDRANT_SECRET_NAME)): "fictitious-qdrant-key\n",
        os.fspath(secrets.secret_path(OPENSEARCH_PASSWORD_SECRET_NAME)): "fictitious-opensearch-password\n",
    }


def empty_store_result(
    command: Sequence[str], request: str | None, rendered_dir: str
) -> subprocess.CompletedProcess[str] | None:
    """Answer the two stores' empty listings and the repository registration."""

    argv = tuple(command)
    if argv == (
        "find", QDRANT_SNAPSHOTS_ROOT, "-mindepth", "1", "-maxdepth", "1",
        "-type", "d", "-printf", "%f\\0",
    ):
        return subprocess.CompletedProcess(list(argv), 0, "", "")
    qdrant_exec = tuple(stack.exec_argv(rendered_dir, QDRANT_SERVICE_NAME))
    if argv[:len(qdrant_exec)] == qdrant_exec:
        assert request is not None
        assert request.startswith("GET /collections HTTP/1.0\r\n")
        body = json.dumps({"result": {"collections": []}})
        reply = f"HTTP/1.0 200 OK\r\ncontent-length: {len(body)}\r\n\r\n{body}"
        return subprocess.CompletedProcess(list(argv), 0, reply, "")
    opensearch_exec = tuple(stack.exec_argv(rendered_dir, OPENSEARCH_SERVICE_NAME))
    if argv[:len(opensearch_exec)] == opensearch_exec:
        assert request is not None
        paths: dict[tuple[str, str], object] = {
            ("PUT", f"/_snapshot/{OPENSEARCH_REPOSITORY}"): {"acknowledged": True},
            ("GET", "/_cat/indices/*,-.*?format=json&expand_wildcards=open,hidden"): [],
            ("GET", f"/_snapshot/{OPENSEARCH_REPOSITORY}/_all"): {"snapshots": []},
        }
        for (method, path), payload in paths.items():
            if f'request = "{method}"' in request and f"127.0.0.1:9200{path}" in request:
                reply = json.dumps(payload) + "\ngideon-status:200\n"
                return subprocess.CompletedProcess(list(argv), 0, reply, "")
        raise AssertionError("unexpected OpenSearch request")
    return None


def recovery_store_result(
    command: Sequence[str], request: str | None, rendered_dir: str, *,
    qdrant_files: Sequence[str] = (),
    opensearch_snapshots: Sequence[Mapping[str, object]] = (),
    qdrant_status: int = 200,
    restore_failed: int = 0,
) -> subprocess.CompletedProcess[str] | None:
    """Answer the restored roots' walk and the two stores' recovery calls."""

    argv = tuple(command)
    if argv == (
        "find", QDRANT_SNAPSHOTS_ROOT, "-mindepth", "2", "-maxdepth", "2",
        "-type", "f", "-name", "*.snapshot", "-printf", "%P\\0",
    ):
        listing = "".join(f"{name}\0" for name in qdrant_files)
        return subprocess.CompletedProcess(list(argv), 0, listing, "")
    qdrant_exec = tuple(stack.exec_argv(rendered_dir, QDRANT_SERVICE_NAME))
    if argv[:len(qdrant_exec)] == qdrant_exec:
        assert request is not None
        assert request.startswith("PUT /collections/")
        payload: object = (
            {"result": True} if qdrant_status == 200
            else {"status": {"error": "recovery refused"}}
        )
        body = json.dumps(payload)
        reply = f"HTTP/1.0 {qdrant_status} Reply\r\ncontent-length: {len(body)}\r\n\r\n{body}"
        return subprocess.CompletedProcess(list(argv), 0, reply, "")
    opensearch_exec = tuple(stack.exec_argv(rendered_dir, OPENSEARCH_SERVICE_NAME))
    if argv[:len(opensearch_exec)] == opensearch_exec:
        assert request is not None
        repository = f"/_snapshot/{OPENSEARCH_REPOSITORY}"
        paths: dict[tuple[str, str], tuple[int, object]] = {
            ("DELETE", repository): (404, {"error": "missing"}),
            ("PUT", repository): (200, {"acknowledged": True}),
            ("GET", f"{repository}/_all"): (200, {"snapshots": list(opensearch_snapshots)}),
        }
        for (method, path), (status, payload) in paths.items():
            if f'request = "{method}"' in request and f"127.0.0.1:9200{path}" in request:
                reply = json.dumps(payload) + f"\ngideon-status:{status}\n"
                return subprocess.CompletedProcess(list(argv), 0, reply, "")
        if 'request = "DELETE"' in request:
            reply = '{"error":"missing"}\ngideon-status:404\n'
            return subprocess.CompletedProcess(list(argv), 0, reply, "")
        if 'request = "POST"' in request and "_restore?wait_for_completion=true" in request:
            reply = json.dumps({"snapshot": {"shards": {"failed": restore_failed}}})
            return subprocess.CompletedProcess(list(argv), 0, reply + "\ngideon-status:200\n", "")
        raise AssertionError("unexpected OpenSearch recovery request")
    return None
