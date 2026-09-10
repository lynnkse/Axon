from __future__ import annotations

import io
import json
import logging
import socket
import threading
import unittest
from unittest.mock import patch

from sandbox.policy import PolicyError, validate_policy
from sandbox.research_proxy import (
    FetchError, HTTPSPinnedTransport, PinnedHTTPSConnection, ResearchFetcher,
    ResearchProxyServer, SafeAudit, Target, UpstreamResponse, parse_target,
    resolve_public, validate_public_ip,
)


def policy_document(**changes):
    document = {
        "schema_version": 1,
        "policy_id": "test-research",
        "allowed_hosts": ["example.com", "redirect.example"],
        "allowed_content_types": ["text/html", "text/plain", "application/json"],
        "limits": {
            "request_bytes": 1024,
            "response_bytes": 4096,
            "redirects": 2,
            "timeout_seconds": 5,
            "concurrency": 2,
            "requests_per_minute": 20,
        },
    }
    document.update(changes)
    return document


class PolicyTests(unittest.TestCase):
    def test_valid_policy_is_canonical_and_hashed(self):
        policy = validate_policy(policy_document(allowed_hosts=["EXAMPLE.com."]))
        self.assertEqual(policy.allowed_hosts, frozenset({"example.com"}))
        self.assertEqual(len(policy.policy_hash), 64)

    def test_unknown_or_missing_keys_fail_closed(self):
        for mutation in ("unknown", "missing"):
            document = policy_document()
            if mutation == "unknown":
                document["permit_all"] = True
            else:
                del document["limits"]
            with self.subTest(mutation=mutation), self.assertRaises(PolicyError):
                validate_policy(document)

    def test_boolean_is_not_accepted_as_integer(self):
        document = policy_document()
        document["limits"]["redirects"] = True
        with self.assertRaises(PolicyError):
            validate_policy(document)

    def test_limits_cannot_exceed_framework_ceilings(self):
        for key, value in (("request_bytes", 8193), ("response_bytes", 5242881),
                           ("redirects", 4), ("timeout_seconds", 16),
                           ("concurrency", 9), ("requests_per_minute", 121)):
            document = policy_document()
            document["limits"][key] = value
            with self.subTest(key=key), self.assertRaises(PolicyError):
                validate_policy(document)

    def test_pdf_is_invalid_in_v1_policy(self):
        with self.assertRaises(PolicyError):
            validate_policy(policy_document(allowed_content_types=["application/pdf"]))

    def test_duplicate_hosts_after_canonicalization_fail(self):
        with self.assertRaises(PolicyError):
            validate_policy(policy_document(allowed_hosts=["example.com", "EXAMPLE.COM."]))


class URLTests(unittest.TestCase):
    def setUp(self):
        self.policy = validate_policy(policy_document())

    def test_valid_url_is_canonicalized(self):
        target = parse_target("https://EXAMPLE.com./a?q=1", self.policy)
        self.assertEqual((target.host, target.port, target.path), ("example.com", 443, "/a?q=1"))

    def test_url_attack_forms_are_denied(self):
        attacks = [
            "http://example.com/", "https://example.com:444/", "https://user@example.com/",
            "https://example.com/#secret", "https://127.0.0.1/", "https://[::1]/",
            "https://evil.example/", " https://example.com/", "https://example.com/a\\b",
            "https://example.com//evil", "https://example.com\n.evil/", "//example.com/",
        ]
        for url in attacks:
            with self.subTest(url=url), self.assertRaises(FetchError) as caught:
                parse_target(url, self.policy)
            self.assertEqual(caught.exception.reason, "destination_denied")

    def test_exact_host_matching_does_not_grant_subdomains(self):
        with self.assertRaises(FetchError):
            parse_target("https://sub.example.com/", self.policy)


class DNSAndPinningTests(unittest.TestCase):
    def test_private_and_special_ranges_are_denied(self):
        denied = ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1",
                  "192.0.2.1", "224.0.0.1", "::1", "fe80::1", "fc00::1", "2001:db8::1"]
        for address in denied:
            with self.subTest(address=address), self.assertRaises(FetchError):
                validate_public_ip(address)
        self.assertEqual(str(validate_public_ip("93.184.216.34")), "93.184.216.34")
        self.assertEqual(str(validate_public_ip("2606:4700:4700::1111")), "2606:4700:4700::1111")

    def test_every_multi_address_dns_result_is_validated(self):
        def mixed(*_args, **_kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ]
        with self.assertRaises(FetchError) as caught:
            resolve_public("example.com", 443, mixed)
        self.assertEqual(caught.exception.reason, "destination_denied")

    def test_addresses_are_deduplicated_without_dropping_ipv6(self):
        def answers(*_args, **_kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
            ]
        self.assertEqual(resolve_public("example.com", 443, answers),
                         ("93.184.216.34", "2606:4700:4700::1111"))

    def test_connection_uses_pinned_ip_and_original_tls_hostname(self):
        calls = {}

        class Raw:
            def close(self): calls["closed"] = True

        class Context:
            verify_mode = __import__("ssl").CERT_REQUIRED
            check_hostname = True
            post_handshake_auth = False
            def wrap_socket(self, raw, server_hostname):
                calls["server_hostname"] = server_hostname
                return raw

        with patch("sandbox.research_proxy.socket.create_connection", return_value=Raw()) as connect:
            connection = PinnedHTTPSConnection("example.com", "93.184.216.34", 443, 3, Context())
            connection.connect()
        connect.assert_called_once_with(("93.184.216.34", 443), 3)
        self.assertEqual(calls["server_hostname"], "example.com")

    def test_dns_is_resolved_once_then_pinned_for_request(self):
        resolver_calls = []
        def resolver(host, port, type):
            resolver_calls.append((host, port, type))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        class FakeConnection:
            def __init__(self, host, pinned_ip, port, timeout, context):
                self.pinned_ip = pinned_ip
            def request(self, *_args, **_kwargs): pass
            def getresponse(self):
                class Response:
                    status = 200
                    def getheaders(self): return [("Content-Type", "text/plain")]
                    def read(self, amount): return b"ok"
                return Response()
            def close(self): pass

        with patch("sandbox.research_proxy.PinnedHTTPSConnection", FakeConnection):
            response = HTTPSPinnedTransport(resolver=resolver).request(
                Target("https://example.com/", "example.com", 443, "/"), 10**12, 10)
        self.assertEqual(resolver_calls, [("example.com", 443, socket.SOCK_STREAM)])
        self.assertEqual(response.peer_ip, "93.184.216.34")

    def test_transport_rejects_declared_oversize_compression_and_stream_overflow(self):
        def resolver(*_args, **_kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        def run(headers, body, limit=10):
            class FakeConnection:
                def __init__(self, *_args, **_kwargs): pass
                def request(self, *_args, **_kwargs): pass
                def getresponse(self):
                    class Response:
                        status = 200
                        def getheaders(self): return list(headers.items())
                        def read(self, amount):
                            self.amount = amount
                            return body[:amount]
                    return Response()
                def close(self): pass
            with patch("sandbox.research_proxy.PinnedHTTPSConnection", FakeConnection):
                return HTTPSPinnedTransport(resolver=resolver).request(
                    Target("https://example.com/", "example.com", 443, "/"), 10**12, limit)

        cases = [
            ({"content-type": "text/plain", "content-length": "11"}, b"small"),
            ({"content-type": "text/plain", "content-encoding": "gzip"}, b"small"),
            ({"content-type": "text/plain"}, b"x" * 11),
        ]
        for headers, body in cases:
            with self.subTest(headers=headers), self.assertRaises(FetchError) as caught:
                run(headers, body)
            self.assertEqual(caught.exception.reason, "response_too_large")


class SequenceTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.targets = []

    def request(self, target, deadline, response_limit):
        self.targets.append(target)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if len(response.body) > response_limit:
            raise FetchError("response_too_large")
        return response


def upstream(status=200, headers=None, body=b"ok"):
    return UpstreamResponse(status, headers or {"content-type": "text/plain"}, body, "93.184.216.34")


class RedirectAndResponseTests(unittest.TestCase):
    def setUp(self):
        self.policy = validate_policy(policy_document())

    def test_redirect_is_followed_manually_after_new_policy_validation(self):
        transport = SequenceTransport([
            upstream(302, {"location": "https://redirect.example/final"}, b""),
            upstream(body=b"done"),
        ])
        result = ResearchFetcher(self.policy, transport).fetch("https://example.com/start")
        self.assertEqual([target.host for target in transport.targets], ["example.com", "redirect.example"])
        self.assertEqual(result["redirects"], 1)

    def test_redirect_to_private_ip_or_unlisted_host_is_denied_before_second_request(self):
        for location in ("https://127.0.0.1/admin", "https://evil.example/admin"):
            transport = SequenceTransport([upstream(302, {"location": location}, b"")])
            with self.subTest(location=location), self.assertRaises(FetchError):
                ResearchFetcher(self.policy, transport).fetch("https://example.com/start")
            self.assertEqual(len(transport.targets), 1)

    def test_relative_redirect_stays_on_validated_host(self):
        transport = SequenceTransport([upstream(302, {"location": "/next"}, b""), upstream()])
        ResearchFetcher(self.policy, transport).fetch("https://example.com/start")
        self.assertEqual(transport.targets[1].url, "https://example.com/next")

    def test_redirect_limit_is_enforced(self):
        transport = SequenceTransport([upstream(302, {"location": "/again"}, b"")] * 3)
        with self.assertRaises(FetchError) as caught:
            ResearchFetcher(self.policy, transport).fetch("https://example.com/start")
        self.assertEqual(caught.exception.reason, "destination_denied")

    def test_body_limit_and_declared_response_limit(self):
        transport = SequenceTransport([upstream(body=b"x" * 4097)])
        with self.assertRaises(FetchError) as caught:
            ResearchFetcher(self.policy, transport).fetch("https://example.com/")
        self.assertEqual(caught.exception.reason, "response_too_large")

    def test_pdf_and_compressed_content_are_denied(self):
        pdf = SequenceTransport([upstream(headers={"content-type": "application/pdf"}, body=b"%PDF-bad")])
        with self.assertRaises(FetchError):
            ResearchFetcher(self.policy, pdf).fetch("https://example.com/file.pdf")
        mislabeled = SequenceTransport([upstream(headers={"content-type": "text/plain"}, body=b" \n%PDF-bad")])
        with self.assertRaises(FetchError):
            ResearchFetcher(self.policy, mislabeled).fetch("https://example.com/file")


    def test_untrusted_marker_and_provenance_are_returned(self):
        result = ResearchFetcher(self.policy, SequenceTransport([upstream(body=b"hello")])).fetch(
            "https://example.com/")
        self.assertTrue(result["untrusted_content"])
        self.assertEqual(result["response_bytes"], 5)
        self.assertEqual(result["policy_hash"], self.policy.policy_hash)


class AuditTests(unittest.TestCase):
    def test_audit_does_not_log_path_query_credentials_or_body(self):
        stream = io.StringIO()
        logger = logging.getLogger("test.safe.audit")
        logger.handlers = [logging.StreamHandler(stream)]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        policy = validate_policy(policy_document())
        SafeAudit(logger).record(policy=policy,
            url="https://example.com/private/path?token=secret-value", outcome="ok",
            status=200, response_bytes=4, latency_ms=2)
        record = stream.getvalue()
        self.assertIn('"destination_host":"example.com"', record)
        for secret in ("private", "token", "secret-value"):
            self.assertNotIn(secret, record)


class HTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.policy = validate_policy(policy_document())
        self.fetcher = ResearchFetcher(self.policy, SequenceTransport([upstream(body=b"hello")]))
        self.server = ResearchProxyServer(("127.0.0.1", 0), self.policy, self.fetcher)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, raw: bytes) -> bytes:
        with socket.create_connection(self.server.server_address, timeout=2) as client:
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            chunks = []
            while chunk := client.recv(4096):
                chunks.append(chunk)
        return b"".join(chunks)

    def test_end_to_end_valid_request(self):
        body = json.dumps({"url": "https://example.com/"}).encode()
        response = self.request(
            b"POST /v1/fetch HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n\r\n" + body)
        self.assertIn(b"HTTP/1.1 200 OK", response)
        self.assertIn(b'"untrusted_content":true', response)

    def test_oversized_body_is_rejected_without_reading_it(self):
        response = self.request(
            b"POST /v1/fetch HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 1025\r\n\r\n")
        self.assertIn(b"413 Content Too Large", response)

    def test_oversized_request_line_and_headers_are_rejected(self):
        long_path = b"/" + b"x" * 2050
        response = self.request(b"POST " + long_path + b" HTTP/1.1\r\n\r\n")
        self.assertIn(b"400 Bad Request", response)
        oversized_header = b"POST /v1/fetch HTTP/1.1\r\nX-Fill: " + b"x" * 4100 + b"\r\n\r\n"
        response = self.request(oversized_header)
        self.assertIn(b"413 Content Too Large", response)

    def test_chunked_missing_length_duplicate_and_malformed_requests_fail(self):
        cases = [
            b"POST /v1/fetch HTTP/1.1\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n",
            b"POST /v1/fetch HTTP/1.1\r\nContent-Type: application/json\r\n\r\n{}",
            b"POST /v1/fetch HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}",
            b"BROKEN\r\n\r\n",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                response = self.request(raw)
                self.assertRegex(response.split(b"\r\n", 1)[0], br"HTTP/1.1 (400|411)")

    def test_extra_json_fields_and_non_json_are_rejected(self):
        for body, content_type in ((b'{"url":"https://example.com/","headers":{}}', b"application/json"),
                                   (b"not json", b"text/plain")):
            raw = (b"POST /v1/fetch HTTP/1.1\r\nContent-Type: " + content_type
                   + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            with self.subTest(body=body):
                self.assertRegex(self.request(raw).split(b"\r\n", 1)[0], br"HTTP/1.1 (400|415)")


if __name__ == "__main__":
    unittest.main()
