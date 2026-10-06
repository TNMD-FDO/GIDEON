"""Corpus transfers over a temporary snapshots root and fake HTTP."""

import gzip
import hashlib
import io
import json
import logging
import os
import socket
import stat
import tempfile
import threading
import unittest
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

from gideon.worker import fetch, logs


class Unread(httpx.SyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    def __iter__(self) -> Iterator[bytes]:
        yield self.content


class StreamingMockTransport(httpx.MockTransport):
    """Serve each fake answer unread, as the network does, so raw reads work."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = super().handle_request(request)
        if not response.is_stream_consumed:
            return response
        return httpx.Response(
            response.status_code, headers=response.headers,
            stream=Unread(response.content), request=request,
        )


class CutStream(httpx.SyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    def __iter__(self) -> Iterator[bytes]:
        yield self.content
        raise httpx.ReadError("fictitious dropped stream")


class WorkerFetch(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = "fictions-2026-09-30/bulk-data/opinions.json"
        self.url = "https://archive.example.test/bulk-data/opinions.json?private-query"

    def seed_partial(
        self, body: bytes, durable: int, *, etag: str | None = '"fixture-etag"',
        host: str | None = None, path: str | None = None, tail: bytes = b"",
    ) -> tuple[Path, Path, Path]:
        target = self.root / self.destination
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + fetch.PARTIAL_SUFFIX)
        record_path = target.with_name(target.name + fetch.RECORD_SUFFIX)
        partial.write_bytes(body[:durable] + tail)
        url_host, url_path = fetch.validate_url(self.url)
        fetch.write_record(record_path, fetch.FetchRecord(
            "partial", fetch.KEPT_FORM, host or url_host, path or url_path,
            etag, "Tue, 06 Oct 2026 00:00:00 GMT", len(body), durable,
            None, None, None, 7, 2.5, 0,
        ))
        return target, partial, record_path

    def client(self, respond: httpx.MockTransport) -> httpx.Client:
        return httpx.Client(transport=respond)

    def test_whole_file_and_every_record_field(self) -> None:
        body = b"A visibly fictitious corpus object.\n"
        etag = '"fixture-etag"'
        modified = "Tue, 06 Oct 2026 00:00:00 GMT"
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=body,
                headers={"content-length": str(len(body)), "etag": etag,
                         "last-modified": modified},
            )

        instant = datetime(2026, 10, 6, tzinfo=UTC)
        record = fetch.transfer(
            self.root, self.destination, self.url, fetch.KEPT_FORM, 37,
            client_factory=lambda: httpx.Client(transport=StreamingMockTransport(respond)),
            clock=lambda: instant,
        )
        target = self.root / self.destination
        self.assertEqual(target.read_bytes(), body)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url, self.url)
        expected = {
            "schema": 1, "state": "whole", "form": fetch.KEPT_FORM,
            "host": requests[0].url.host, "path": requests[0].url.path,
            "etag": etag, "last_modified": modified, "total": len(body),
            "durable": len(body), "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "fetched_at": instant.isoformat(), "job": 37,
            "seconds": 0.0, "resumes": 0,
        }
        record_path = target.with_name(target.name + fetch.RECORD_SUFFIX)
        self.assertEqual(json.loads(record_path.read_text()), expected)
        self.assertEqual(fetch.read_record(record_path), record)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), fetch.FILE_MODE)
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), fetch.FILE_MODE)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), fetch.DIR_MODE)
        self.assertEqual(stat.S_IMODE(target.parent.parent.stat().st_mode), fetch.DIR_MODE)
        self.assertFalse(target.with_name(target.name + fetch.PARTIAL_SUFFIX).exists())

    def test_an_encoded_answer_is_kept_as_served_and_identity_is_asked(self) -> None:
        served = gzip.compress(b"fictitious corpus line\n" * 50)
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, stream=Unread(served), headers={
                "Content-Encoding": "gzip", "Content-Length": str(len(served)),
                "ETag": '"fixture-etag"',
            })

        record = fetch.transfer(
            self.root, self.destination, self.url, fetch.KEPT_FORM, 3,
            client_factory=lambda: fetch._client(StreamingMockTransport(respond)),
        )
        self.assertEqual(requests[0].headers["Accept-Encoding"], "identity")
        self.assertEqual((self.root / self.destination).read_bytes(), served)
        self.assertEqual((record.size, record.sha256),
                         (len(served), hashlib.sha256(served).hexdigest()))

    def test_a_stale_read_only_temporary_does_not_block_the_next_record(self) -> None:
        body = b"fictitious object after an interrupted record write"
        target = self.root / self.destination
        target.parent.mkdir(parents=True)
        stale = target.with_name(target.name + fetch.RECORD_SUFFIX + fetch.TEMP_SUFFIX)
        stale.write_text("half a record")
        stale.chmod(0o440)

        record = fetch.transfer(
            self.root, self.destination, self.url, fetch.KEPT_FORM, 4,
            client_factory=lambda: self.client(StreamingMockTransport(
                lambda request: httpx.Response(200, content=body))),
        )
        self.assertEqual(record.sha256, hashlib.sha256(body).hexdigest())
        self.assertFalse(stale.exists())

    def test_destination_and_url_grammars_refuse_before_request(self) -> None:
        bad_destinations = (
            "", "one", "resolve/file", "other/file", "fictions-2026-09-30",
            "fictions-2026-09-30/../outside", "fictions-2026-09-30/.hidden",
            "fictions-2026-09-30/file.fetch.json",
            "fictions-2026-13-30/file",
            "fictions-2026-09-30/" + "/".join(["part"] * 8),
        )
        for destination in bad_destinations:
            with self.subTest(destination=destination), self.assertRaises(fetch.FetchFailure):
                fetch.transfer(self.root, destination, self.url, fetch.KEPT_FORM, 1)
        for url in (
            "http://archive.example.test/file", "https://UPPER.example.test/file",
            "https://user@archive.example.test/file",
            "https://archive.example.test:444/file",
            "https://archive.example.test/file#fragment",
            "https://archive.example.test/file with spaces",
            "https://archive.example.test/" + "a" * fetch.URL_MAX_LENGTH,
        ):
            with self.subTest(url=url), self.assertRaises(fetch.FetchFailure):
                fetch.transfer(self.root, self.destination, url, fetch.KEPT_FORM, 1)
        fetch.validate_destination("resolve/index.json", fetch.FRESH_FORM)
        with self.assertRaises(fetch.FetchFailure):
            fetch.validate_destination(self.destination, fetch.FRESH_FORM)

    def test_path_leaving_root_and_symbolic_link_are_refused(self) -> None:
        with self.assertRaises(fetch.FetchFailure):
            fetch.destination_path(self.root, "../outside/file", fetch.KEPT_FORM)
        outside = self.root.parent / "outside"
        (self.root / "fictions-2026-09-30").symlink_to(outside)
        with self.assertRaises(fetch.FetchFailure):
            fetch.destination_path(self.root, self.destination, fetch.KEPT_FORM)

    def test_failure_record_round_trip(self) -> None:
        path = self.root / "failure.json"
        record = fetch.FetchFailureRecord(
            9, "upstream-status", "archive.example.test", 404, None,
            datetime(2026, 10, 6, tzinfo=UTC).isoformat(),
        )
        fetch.write_failure(path, record)
        self.assertEqual(fetch.read_failure(path), record)
        self.assertEqual(json.loads(path.read_text())["status"], 404)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), fetch.PARTIAL_MODE)

    def test_resume_truncates_tail_and_hashes_kept_bytes(self) -> None:
        body = b"A fictitious object with a known tail"
        offset = len(body) // 2
        target, partial, _ = self.seed_partial(body, offset, tail=b"untrusted tail")
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(206, content=body[offset:], headers={
                "Content-Range": f"bytes {offset}-{len(body) - 1}/{len(body)}",
                "Content-Length": str(len(body) - offset),
                "ETag": '"fixture-etag"', "Accept-Ranges": "bytes",
                "Last-Modified": "Tue, 06 Oct 2026 00:00:00 GMT",
            })

        record = fetch.transfer(
            self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
            client_factory=lambda: self.client(StreamingMockTransport(respond)),
        )
        self.assertEqual(target.read_bytes(), body)
        self.assertFalse(partial.exists())
        self.assertEqual(record.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual((record.durable, record.size, record.total),
                         (len(body), len(body), len(body)))
        self.assertEqual(record.resumes, 1)
        self.assertGreaterEqual(record.seconds, 2.5)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["Range"], f"bytes={offset}-")
        self.assertEqual(requests[0].headers["If-Match"], '"fixture-etag"')

    def test_changed_range_answers_discard_incomplete_bytes(self) -> None:
        body = b"fictitious source object"
        offset = len(body) // 2
        answers: tuple[tuple[int, dict[str, str], bytes], ...] = (
            (412, {}, b""),
            (200, {"Content-Length": str(len(body))}, body),
            (416, {}, b""),
            (206, {"Content-Range": f"bytes {offset}-{len(body)-1}/{len(body)}",
                   "ETag": '"different-etag"'}, body[offset:]),
            (206, {"Content-Range": f"bytes {offset+1}-{len(body)-1}/{len(body)}",
                   "ETag": '"fixture-etag"'}, body[offset+1:]),
            (206, {"Content-Range": f"bytes {offset}-{len(body)-1}/{len(body)+1}",
                   "ETag": '"fixture-etag"'}, body[offset:]),
        )
        for status, headers, content in answers:
            with self.subTest(status=status, headers=headers):
                target, partial, record_path = self.seed_partial(body, offset)
                def respond(
                    request: httpx.Request, current_status: int = status,
                    current_headers: dict[str, str] = headers,
                    current_content: bytes = content,
                ) -> httpx.Response:
                    return httpx.Response(
                        current_status, headers=current_headers, content=current_content
                    )

                response = StreamingMockTransport(respond)

                def make_client(current_response: httpx.MockTransport = response) -> httpx.Client:
                    return self.client(current_response)

                with self.assertRaises(fetch.FetchFailure) as raised:
                    fetch.transfer(
                        self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                        client_factory=make_client,
                    )
                self.assertEqual(raised.exception.reason, "changed")
                self.assertEqual(raised.exception.host, fetch.validate_url(self.url)[0])
                self.assertFalse(partial.exists())
                self.assertFalse(record_path.exists())
                self.assertFalse(target.exists())

    def test_unrelated_partial_restarts_at_zero(self) -> None:
        body = b"fictitious replacement object"
        for etag, host, path in (
            (None, None, None),
            ('"fixture-etag"', "other.example.test", None),
            ('"fixture-etag"', None, "/different/path"),
        ):
            with self.subTest(etag=etag, host=host, path=path):
                target, _, _ = self.seed_partial(
                    body, len(body) // 2, etag=etag, host=host, path=path,
                )
                requests: list[httpx.Request] = []

                def respond(
                    request: httpx.Request, current_requests: list[httpx.Request] = requests,
                ) -> httpx.Response:
                    current_requests.append(request)
                    return httpx.Response(200, content=body, headers={
                        "Content-Length": str(len(body)), "ETag": '"fixture-etag"',
                    })

                record = fetch.transfer(
                    self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                    client_factory=lambda: self.client(StreamingMockTransport(respond)),
                )
                self.assertEqual(target.read_bytes(), body)
                self.assertEqual(record.resumes, 0)
                self.assertEqual(len(requests), 1)
                self.assertNotIn("Range", requests[0].headers)
                target.unlink()
                target.with_name(target.name + fetch.RECORD_SUFFIX).unlink()

    def test_short_first_body_retries_by_range_from_current_offset(self) -> None:
        body = b"fictitious body cut short then completed"
        offset = len(body) // 2
        requests: list[httpx.Request] = []
        sleeps: list[float] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, content=body[:offset], headers={
                    "Content-Length": str(len(body)), "ETag": '"fixture-etag"',
                })
            resumed = int(request.headers["Range"].removeprefix("bytes=").removesuffix("-"))
            return httpx.Response(206, content=body[resumed:], headers={
                "Content-Range": f"bytes {resumed}-{len(body)-1}/{len(body)}",
                "ETag": '"fixture-etag"', "Accept-Ranges": "bytes",
            })

        record = fetch.transfer(
            self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
            client_factory=lambda: self.client(StreamingMockTransport(respond)),
            sleep=sleeps.append,
        )
        self.assertEqual((self.root / self.destination).read_bytes(), body)
        self.assertEqual(record.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(record.resumes, 1)
        self.assertEqual(requests[1].headers["Range"], f"bytes={offset}-")
        self.assertEqual(requests[1].headers["If-Match"], '"fixture-etag"')
        self.assertEqual(sleeps, [fetch.RETRY_WAITS_SECONDS[0]])

    def test_dropped_stream_retries_from_bytes_gained_in_this_run(self) -> None:
        body = b"fictitious dropped connection and safe resumed bytes"
        offset = len(body) // 2
        requests: list[httpx.Request] = []
        sleeps: list[float] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, stream=CutStream(body[:offset]), headers={
                    "Content-Length": str(len(body)), "ETag": '"fixture-etag"',
                })
            resumed = int(request.headers["Range"].removeprefix("bytes=").removesuffix("-"))
            return httpx.Response(206, content=body[resumed:], headers={
                "Content-Range": f"bytes {resumed}-{len(body)-1}/{len(body)}",
                "ETag": '"fixture-etag"', "Accept-Ranges": "bytes",
            })

        with patch.object(fetch, "CHUNK_BYTES", 4):
            record = fetch.transfer(
                self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                client_factory=lambda: self.client(StreamingMockTransport(respond)),
                sleep=sleeps.append,
            )
        self.assertEqual((self.root / self.destination).read_bytes(), body)
        self.assertEqual(record.sha256, hashlib.sha256(body).hexdigest())
        resumed = int(requests[1].headers["Range"].removeprefix("bytes=").removesuffix("-"))
        self.assertGreater(resumed, 0)
        self.assertLessEqual(resumed, offset)
        self.assertEqual(sleeps, [fetch.RETRY_WAITS_SECONDS[0]])

    def test_retryable_statuses_keep_checkpoint_and_use_injected_sleep(self) -> None:
        body = b"fictitious checkpoint plus remainder"
        offset = len(body) // 2
        for status in (503, 408, 429):
            with self.subTest(status=status):
                target, _, _ = self.seed_partial(body, offset)
                requests: list[httpx.Request] = []
                sleeps: list[float] = []

                def respond(
                    request: httpx.Request, current_requests: list[httpx.Request] = requests,
                    current_status: int = status,
                ) -> httpx.Response:
                    current_requests.append(request)
                    if len(current_requests) == 1:
                        return httpx.Response(current_status)
                    return httpx.Response(206, content=body[offset:], headers={
                        "Content-Range": f"bytes {offset}-{len(body)-1}/{len(body)}",
                        "ETag": '"fixture-etag"', "Accept-Ranges": "bytes",
                    })

                record = fetch.transfer(
                    self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                    client_factory=lambda: self.client(StreamingMockTransport(respond)),
                    sleep=sleeps.append,
                )
                self.assertEqual(target.read_bytes(), body)
                self.assertEqual(record.sha256, hashlib.sha256(body).hexdigest())
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[0].headers["Range"], f"bytes={offset}-")
                self.assertEqual(requests[1].headers["Range"], f"bytes={offset}-")
                self.assertEqual(sleeps, [fetch.RETRY_WAITS_SECONDS[0]])
                target.unlink()
                target.with_name(target.name + fetch.RECORD_SUFFIX).unlink()

    def test_retry_bound_and_other_status_preserve_checkpoint(self) -> None:
        body = b"fictitious checkpoint and remainder"
        offset = len(body) // 2
        for status, count in ((503, fetch.MAX_NO_PROGRESS_FAILURES), (404, 1)):
            with self.subTest(status=status):
                _, partial, record_path = self.seed_partial(body, offset)
                original_record = record_path.read_bytes()
                requests: list[httpx.Request] = []
                sleeps: list[float] = []

                def respond(
                    request: httpx.Request, current_requests: list[httpx.Request] = requests,
                    current_status: int = status,
                ) -> httpx.Response:
                    current_requests.append(request)
                    return httpx.Response(current_status)

                with self.assertRaises(fetch.FetchFailure) as raised:
                    fetch.transfer(
                        self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                        client_factory=lambda: self.client(StreamingMockTransport(respond)),
                        sleep=sleeps.append,
                    )
                self.assertEqual(raised.exception.reason, "upstream-status")
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(partial.read_bytes(), body[:offset])
                self.assertEqual(record_path.read_bytes(), original_record)
                self.assertEqual(len(requests), count)
                self.assertEqual(len(sleeps), count - 1)
                partial.unlink()
                record_path.unlink()

    def test_repeated_short_answers_without_etag_hit_retry_bound(self) -> None:
        body = b"fictitious incomplete object"
        requests: list[httpx.Request] = []
        sleeps: list[float] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=body[:4], headers={
                "Content-Length": str(len(body)),
            })

        with self.assertRaises(fetch.FetchFailure) as raised:
            fetch.transfer(
                self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                client_factory=lambda: self.client(StreamingMockTransport(respond)),
                sleep=sleeps.append,
            )
        self.assertEqual(raised.exception.reason, "transport")
        self.assertEqual(sleeps, list(fetch.RETRY_WAITS_SECONDS))
        self.assertEqual(len(sleeps), fetch.MAX_NO_PROGRESS_FAILURES - 1)
        self.assertTrue(all("Range" not in request.headers for request in requests))

    def test_completion_interrupted_after_flush_resumes_from_checkpoint(self) -> None:
        body = b"fictitious"
        target = self.root / self.destination
        record_path = target.with_name(target.name + fetch.RECORD_SUFFIX)
        partial = target.with_name(target.name + fetch.PARTIAL_SUFFIX)
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, content=body, headers={
                    "Content-Length": str(len(body)), "ETag": '"fixture-etag"',
                })
            offset = int(request.headers["Range"].removeprefix("bytes=").removesuffix("-"))
            return httpx.Response(206, content=body[offset:], headers={
                "Content-Range": f"bytes {offset}-{len(body)-1}/{len(body)}",
                "ETag": '"fixture-etag"', "Accept-Ranges": "bytes",
            })

        def stop(step: str) -> None:
            if step == "flushed":
                raise RuntimeError("fictitious stop")

        transport = StreamingMockTransport(respond)
        with patch.object(fetch, "CHECKPOINT_BYTES", 5), patch.object(fetch, "CHUNK_BYTES", 3):
            with self.assertRaisesRegex(RuntimeError, "fictitious stop"):
                fetch.transfer(
                    self.root, self.destination, self.url, fetch.KEPT_FORM, 7,
                    client_factory=lambda: self.client(transport), on_step=stop,
                )
            checkpoint = fetch.read_record(record_path)
            assert checkpoint is not None
            self.assertEqual(checkpoint.state, "partial")
            self.assertLess(checkpoint.durable, len(body))
            self.assertEqual(partial.read_bytes(), body)
            record = fetch.transfer(
                self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                client_factory=lambda: self.client(transport),
            )
        self.assertEqual(requests[1].headers["Range"], f"bytes={checkpoint.durable}-")
        self.assertEqual(target.read_bytes(), body)
        self.assertEqual(record.sha256, hashlib.sha256(body).hexdigest())

    def test_whole_record_and_rename_interruptions_finish_without_request(self) -> None:
        body = b"fictitious complete object"
        for stopped_at in ("recorded", "renamed"):
            with self.subTest(stopped_at=stopped_at):
                target = self.root / self.destination
                partial = target.with_name(target.name + fetch.PARTIAL_SUFFIX)
                record_path = target.with_name(target.name + fetch.RECORD_SUFFIX)
                calls: list[httpx.Request] = []

                def respond(
                    request: httpx.Request, current_calls: list[httpx.Request] = calls,
                ) -> httpx.Response:
                    current_calls.append(request)
                    return httpx.Response(200, content=body, headers={
                        "Content-Length": str(len(body)), "ETag": '"fixture-etag"',
                    })

                def stop(step: str, current_stop: str = stopped_at) -> None:
                    if step == current_stop:
                        raise RuntimeError("fictitious stop")

                with self.assertRaisesRegex(RuntimeError, "fictitious stop"):
                    fetch.transfer(
                        self.root, self.destination, self.url, fetch.KEPT_FORM, 7,
                        client_factory=lambda: self.client(StreamingMockTransport(respond)),
                        on_step=stop,
                    )
                whole = fetch.read_record(record_path)
                assert whole is not None
                self.assertEqual(whole.state, "whole")
                self.assertEqual((partial.exists(), target.exists()),
                                 (stopped_at == "recorded", stopped_at == "renamed"))
                original_flush = fetch._flush_directory
                directories: list[Path] = []

                def observe(
                    directory: Path, current_directories: list[Path] = directories,
                    flush: Callable[[Path], None] = original_flush,
                ) -> None:
                    current_directories.append(directory)
                    flush(directory)

                with patch.object(fetch, "_flush_directory", side_effect=observe):
                    recovered = fetch.transfer(
                        self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                        client_factory=lambda: self.client(StreamingMockTransport(respond)),
                    )
                self.assertEqual(recovered, whole)
                self.assertEqual(len(calls), 1)
                self.assertEqual(target.read_bytes(), body)
                self.assertIn(target.parent, directories)
                target.unlink()
                record_path.unlink()

    def test_whole_kept_file_never_fetches_again(self) -> None:
        body = b"fictitious kept file"
        respond = StreamingMockTransport(lambda request: httpx.Response(200, content=body))
        first = fetch.transfer(
            self.root, self.destination, self.url, fetch.KEPT_FORM, 7,
            client_factory=lambda: self.client(respond),
        )

        def refuse_request(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"unexpected request: {request.method}")

        again = fetch.transfer(
            self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
            client_factory=lambda: self.client(StreamingMockTransport(refuse_request)),
        )
        self.assertEqual(again, first)
        self.assertEqual((self.root / self.destination).read_bytes(), body)

    def test_fresh_form_replaces_file_and_never_resumes_across_jobs(self) -> None:
        destination = "resolve/index.json"
        bodies = [b"fictitious first index", b"fictitious replacement index"]
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            body = bodies[len(requests) - 1]
            return httpx.Response(200, content=body, headers={
                "Content-Length": str(len(body)), "ETag": '"fixture-etag"',
            })

        transport = StreamingMockTransport(respond)
        first = fetch.transfer(
            self.root, destination, self.url, fetch.FRESH_FORM, 7,
            client_factory=lambda: self.client(transport),
        )
        second = fetch.transfer(
            self.root, destination, self.url, fetch.FRESH_FORM, 8,
            client_factory=lambda: self.client(transport),
        )
        self.assertEqual((self.root / destination).read_bytes(), bodies[1])
        self.assertEqual(first.sha256, hashlib.sha256(bodies[0]).hexdigest())
        self.assertEqual(second.sha256, hashlib.sha256(bodies[1]).hexdigest())
        self.assertEqual((first.job, second.job), (7, 8))
        self.assertEqual(len(requests), 2)
        self.assertNotIn("Range", requests[1].headers)

    def test_fresh_form_discards_a_prior_jobs_partial(self) -> None:
        destination = "resolve/index.json"
        body = b"fictitious fresh index"
        target = self.root / destination
        target.parent.mkdir()
        partial = target.with_name(target.name + fetch.PARTIAL_SUFFIX)
        record_path = target.with_name(target.name + fetch.RECORD_SUFFIX)
        partial.write_bytes(b"stale bytes")
        host, path = fetch.validate_url(self.url)
        fetch.write_record(record_path, fetch.FetchRecord(
            "partial", fetch.FRESH_FORM, host, path, '"fixture-etag"',
            None, len(body), len(b"stale bytes"), None, None, None, 7, 1.0, 0,
        ))
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=body, headers={
                "Content-Length": str(len(body)), "ETag": '"fixture-etag"',
            })

        record = fetch.transfer(
            self.root, destination, self.url, fetch.FRESH_FORM, 8,
            client_factory=lambda: self.client(StreamingMockTransport(respond)),
        )
        self.assertEqual(target.read_bytes(), body)
        self.assertEqual(record.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(record.resumes, 0)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("Range", requests[0].headers)

    def test_partial_flush_precedes_whole_record_write(self) -> None:
        body = b"fictitious flush order"
        target = self.root / self.destination
        partial = target.with_name(target.name + fetch.PARTIAL_SUFFIX)
        events: list[str] = []
        original_fsync = os.fsync
        original_write = fetch.write_record

        def observe_fsync(descriptor: int) -> None:
            if os.readlink(f"/proc/self/fd/{descriptor}") == str(partial):
                events.append("partial_flushed")
            original_fsync(descriptor)

        def observe_record(path: Path, record: fetch.FetchRecord) -> None:
            events.append(f"{record.state}_record")
            original_write(path, record)

        with (
            patch.object(fetch.os, "fsync", side_effect=observe_fsync),
            patch.object(fetch, "write_record", side_effect=observe_record),
            patch.object(fetch, "CHECKPOINT_BYTES", 5),
            patch.object(fetch, "CHUNK_BYTES", 3),
        ):
            fetch.transfer(
                self.root, self.destination, self.url, fetch.KEPT_FORM, 7,
                client_factory=lambda: self.client(StreamingMockTransport(
                    lambda request: httpx.Response(200, content=body, headers={
                        "Content-Length": str(len(body)), "ETag": '"fixture-etag"',
                    })
                )),
            )
        self.assertIn("partial_flushed", events)
        self.assertIn("partial_record", events)
        for index, event in enumerate(events):
            if event == "partial_record":
                self.assertEqual(events[index - 1], "partial_flushed")
        self.assertLess(events.index("partial_flushed"), events.index("whole_record"))

    def test_redirects_keep_range_conditions_and_reject_unsafe_or_long_chains(self) -> None:
        body = b"fictitious redirected object"
        offset = len(body) // 2
        target, _, _ = self.seed_partial(body, offset)
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "archive.example.test":
                return httpx.Response(302, headers={
                    "Location": "https://other.example.test/object?private-query",
                })
            return httpx.Response(206, content=body[offset:], headers={
                "Content-Range": f"bytes {offset}-{len(body)-1}/{len(body)}",
                "ETag": '"fixture-etag"', "Accept-Ranges": "bytes",
            })

        record = fetch.transfer(
            self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
            client_factory=lambda: self.client(StreamingMockTransport(respond)),
        )
        self.assertEqual(target.read_bytes(), body)
        self.assertEqual(record.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request.headers["Range"], f"bytes={offset}-")
            self.assertEqual(request.headers["If-Match"], '"fixture-etag"')

        target.unlink()
        target.with_name(target.name + fetch.RECORD_SUFFIX).unlink()
        for location in (
            "http://other.example.test/object", "https://UPPER.example.test/object",
            "https://[broken",
        ):
            with self.subTest(location=location):
                def invalid_redirect(
                    request: httpx.Request, target_url: str = location,
                ) -> httpx.Response:
                    return httpx.Response(302, headers={"Location": target_url})

                with self.assertRaises(fetch.FetchFailure) as raised:
                    fetch.transfer(
                        self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                        client_factory=lambda: self.client(StreamingMockTransport(invalid_redirect)),
                    )
                self.assertEqual(raised.exception.reason, "redirect")

        hops: list[httpx.Request] = []

        def loop(request: httpx.Request) -> httpx.Response:
            hops.append(request)
            return httpx.Response(302, headers={"Location": "/again"})

        with self.assertRaises(fetch.FetchFailure) as raised:
            fetch.transfer(
                self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                client_factory=lambda: self.client(StreamingMockTransport(loop)),
            )
        self.assertEqual(raised.exception.reason, "redirect")
        self.assertEqual(len(hops), fetch.MAX_REDIRECTS + 1)

    def test_proxy_refusal_names_each_tunnel_host_without_retry(self) -> None:
        for redirect in (False, True):
            with self.subTest(redirect=redirect):
                requests: list[httpx.Request] = []

                def respond(
                    request: httpx.Request, current_requests: list[httpx.Request] = requests,
                    current_redirect: bool = redirect,
                ) -> httpx.Response:
                    current_requests.append(request)
                    if current_redirect and request.url.host == "archive.example.test":
                        return httpx.Response(302, headers={
                            "Location": "https://blocked.example.test/object",
                        })
                    raise httpx.ProxyError("403 Forbidden; fictitious tunnel detail")

                with self.assertRaises(fetch.FetchFailure) as raised:
                    fetch.transfer(
                        self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                        client_factory=lambda: self.client(StreamingMockTransport(respond)),
                        sleep=lambda seconds: self.fail("proxy refusals must not retry"),
                    )
                self.assertEqual(raised.exception.reason, "refused-host")
                self.assertEqual(raised.exception.status, 403)
                self.assertEqual(
                    raised.exception.host,
                    "blocked.example.test" if redirect else "archive.example.test",
                )
                self.assertEqual(len(requests), 2 if redirect else 1)
                fetch.write_job_failure(
                    self.root, self.destination, fetch.KEPT_FORM, 8, raised.exception,
                )
                failure_path = self.root / (self.destination + fetch.FAILURE_SUFFIX)
                failure = fetch.read_failure(failure_path)
                assert failure is not None
                self.assertEqual(failure.host, raised.exception.host)
                self.assertEqual(
                    sorted(path.name for path in failure_path.parent.iterdir()),
                    [failure_path.name],
                    "Fix: a job that fails before its first byte leaves its failure file alone.",
                )

        def other_proxy_status(request: httpx.Request) -> httpx.Response:
            raise httpx.ProxyError("502 Bad Gateway; fictitious tunnel detail")

        with self.assertRaises(fetch.FetchFailure) as raised:
            fetch.transfer(
                self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                client_factory=lambda: self.client(StreamingMockTransport(other_proxy_status)),
            )
        self.assertEqual((raised.exception.reason, raised.exception.status),
                         ("egress-failed", 502))

    def test_second_writer_is_busy_without_touching_files(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        body = b"fictitious locked object"
        target = self.root / self.destination
        partial = target.with_name(target.name + fetch.PARTIAL_SUFFIX)
        failure_path = target.with_name(target.name + fetch.FAILURE_SUFFIX)

        def wait_for_release(request: httpx.Request) -> httpx.Response:
            entered.set()
            if not release.wait(5):
                raise RuntimeError("fictitious wait expired")
            return httpx.Response(200, content=body)

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                fetch.transfer, self.root, self.destination, self.url, fetch.KEPT_FORM, 7,
                client_factory=lambda: self.client(StreamingMockTransport(wait_for_release)),
            )
            try:
                self.assertTrue(entered.wait(5))
                failure_path.write_bytes(b"stale failure sentinel")
                before = partial.stat()

                def unexpected_client() -> httpx.Client:
                    self.fail("busy writer must not make a request")

                with self.assertRaises(fetch.FetchFailure) as raised:
                    fetch.transfer(
                        self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                        client_factory=unexpected_client,
                    )
                self.assertEqual(raised.exception.reason, "busy")
                fetch.write_job_failure(
                    self.root, self.destination, fetch.KEPT_FORM, 8, raised.exception,
                )
                self.assertEqual(failure_path.read_bytes(), b"stale failure sentinel")
                self.assertEqual(partial.stat().st_ino, before.st_ino)
                self.assertEqual(partial.stat().st_size, before.st_size)
            finally:
                release.set()
            self.assertEqual(first.result().sha256, hashlib.sha256(body).hexdigest())

    def test_real_proxy_connect_refusal_names_the_requested_host(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        self.addCleanup(listener.close)
        request_lines: list[bytes] = []

        def refuse() -> None:
            connection, _ = listener.accept()
            with connection:
                request = b""
                while b"\r\n\r\n" not in request:
                    piece = connection.recv(4096)
                    if not piece:
                        break
                    request += piece
                request_lines.append(request.split(b"\r\n", 1)[0])
                connection.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
                )

        thread = threading.Thread(target=refuse, daemon=True)
        thread.start()
        proxy = f"http://127.0.0.1:{listener.getsockname()[1]}"
        with self.assertRaises(fetch.FetchFailure) as raised:
            fetch.transfer(
                self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                client_factory=lambda: httpx.Client(proxy=proxy, timeout=5, trust_env=False),
            )
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(request_lines, [b"CONNECT archive.example.test:443 HTTP/1.1"])
        self.assertEqual(raised.exception.reason, "refused-host")
        self.assertEqual(raised.exception.host, "archive.example.test")
        self.assertEqual(raised.exception.status, 403)

    def test_production_handler_keeps_private_values_out_of_outputs(self) -> None:
        query = "QUERY_SENTINEL_rare_8e5a"
        header = "HEADER_SENTINEL_rare_f71b"
        exception_text = "EXCEPTION_SENTINEL_rare_491d"
        url = f"https://archive.example.test/object?{query}"
        body = b"fictitious safe object"

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body, headers={
                "X-Private": header, "Content-Length": str(len(body)),
            })

        root_logger = logging.getLogger()
        handlers, level = root_logger.handlers[:], root_logger.level
        bare_output = io.StringIO()
        bare_handler = logging.StreamHandler(bare_output)
        try:
            root_logger.handlers[:] = [bare_handler]
            root_logger.setLevel(logging.INFO)
            with (
                self.client(StreamingMockTransport(respond)) as client,
                client.stream("GET", url) as response,
            ):
                response.read()
            self.assertIn(query, bare_output.getvalue())

            output = io.StringIO()
            logs.install_handler(output)
            record = fetch.transfer(
                self.root, self.destination, url, fetch.KEPT_FORM, 8,
                client_factory=lambda: self.client(StreamingMockTransport(respond)),
            )
            target = self.root / self.destination
            record_path = target.with_name(target.name + fetch.RECORD_SUFFIX)

            def proxy_error(request: httpx.Request) -> httpx.Response:
                raise httpx.ProxyError(f"403 Forbidden; {exception_text}")

            failure_destination = "fictions-2026-09-30/refused"
            with self.assertRaises(fetch.FetchFailure) as raised:
                fetch.transfer(
                    self.root, failure_destination, url, fetch.KEPT_FORM, 9,
                    client_factory=lambda: self.client(StreamingMockTransport(proxy_error)),
                )
            fetch.write_job_failure(
                self.root, failure_destination, fetch.KEPT_FORM, 9, raised.exception,
            )
            failure_path = self.root / (failure_destination + fetch.FAILURE_SUFFIX)
            for value in (query, header, exception_text):
                self.assertNotIn(value, record_path.read_text())
                self.assertNotIn(value, failure_path.read_text())
                self.assertNotIn(value, output.getvalue())
            self.assertIn("action=fetch_start", output.getvalue())
            self.assertIn("action=fetch_end", output.getvalue())
            self.assertIn(record.sha256 or "", output.getvalue())
            self.assertIn("error=ProxyError", output.getvalue())
        finally:
            root_logger.handlers[:] = handlers
            root_logger.setLevel(level)

    def test_transfer_logs_resume_and_progress_at_most_once_per_minute(self) -> None:
        body = b"fictitious specimen!"
        offset = 4
        self.seed_partial(body, offset)
        instant = [datetime(2026, 10, 6, tzinfo=UTC)]

        class TimedStream(httpx.SyncByteStream):
            def __iter__(self) -> Iterator[bytes]:
                for position in range(offset, len(body), 4):
                    instant[0] += timedelta(seconds=30)
                    yield body[position:position + 4]

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(206, stream=TimedStream(), headers={
                "Content-Range": f"bytes {offset}-{len(body)-1}/{len(body)}",
                "ETag": '"fixture-etag"', "Accept-Ranges": "bytes",
            })

        output = io.StringIO()
        root_logger = logging.getLogger()
        handlers, level = root_logger.handlers[:], root_logger.level
        try:
            logs.install_handler(output)
            with patch.object(fetch, "CHUNK_BYTES", 4):
                record = fetch.transfer(
                    self.root, self.destination, self.url, fetch.KEPT_FORM, 8,
                    client_factory=lambda: self.client(StreamingMockTransport(respond)),
                    clock=lambda: instant[0],
                )
        finally:
            root_logger.handlers[:] = handlers
            root_logger.setLevel(level)
        lines = output.getvalue().splitlines()
        self.assertEqual(sum("action=fetch_start" in line for line in lines), 1)
        self.assertEqual(sum("action=fetch_resume" in line for line in lines), 1)
        self.assertEqual(sum("action=fetch_end" in line for line in lines), 1)
        self.assertEqual(sum("action=fetch_progress" in line for line in lines), 2)
        self.assertIn(record.sha256 or "", output.getvalue())


if __name__ == "__main__":
    unittest.main()
