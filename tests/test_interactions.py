"""Unit tests for autoinstall_helper.interactions."""

import urllib.request

from autoinstall_helper.interactions import (
    FakeResponse,
    decode,
    extract_headers,
    extract_method,
    extract_url,
)


class TestFakeResponse:
    """Tests for the urlopen stand-in."""

    def test_reads_body_and_exposes_status(self):
        response = FakeResponse(b"payload", 201, {"Content-Type": "text/plain"})

        assert response.read() == b"payload"
        assert response.status == 201
        assert response.code == 201
        assert response.headers == {"Content-Type": "text/plain"}

    def test_context_manager(self):
        with FakeResponse(b"", 200, {}) as response:
            assert response.read() == b""


class TestExtractors:
    """Tests for URL/method/header extraction."""

    def test_plain_url(self):
        assert extract_url("https://example.com/x") == "https://example.com/x"
        assert extract_method("https://example.com/x") == "GET"
        assert extract_headers("https://example.com/x") == {}

    def test_request(self):
        request = urllib.request.Request(
            "https://example.com/x",
            data=b"body",
            headers={"Snap-Device-Series": "16"},
            method="POST",
        )

        assert extract_url(request) == "https://example.com/x"
        assert extract_method(request) == "POST"
        assert extract_headers(request) == {"Snap-device-series": "16"}


class TestDecode:
    """Tests for defensive output decoding."""

    def test_none_stays_none(self):
        assert decode(None) is None

    def test_text_passthrough(self):
        assert decode("hello") == "hello"

    def test_bytes_are_decoded(self):
        assert decode(b"hello") == "hello"

    def test_invalid_bytes_are_replaced(self):
        assert decode(b"\xff") == "\ufffd"
