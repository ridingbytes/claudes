"""The local page: token, Host check and headers."""

import http.client
import threading

import pytest

import claudes

TOKEN = "t0ken"


@pytest.fixture
def server(claude_home):
    """Serve the page on a free port and return (host, port)."""
    httpd = claudes.http.server.ThreadingHTTPServer(
        (claudes.UI_HOST, 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = claudes.ui_handler(TOKEN, port,
                                                   claudes.UiData())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield claudes.UI_HOST, port
    httpd.shutdown()
    httpd.server_close()


def get(server, path, host=None):
    """Return (status, headers, body) of a GET request."""
    connection = http.client.HTTPConnection(*server)
    connection.putrequest("GET", path, skip_host=True)
    connection.putheader("Host", host or f"{server[0]}:{server[1]}")
    connection.endheaders()
    response = connection.getresponse()
    return response.status, dict(response.getheaders()), response.read()


def test_ui_route_needs_token():
    assert claudes.ui_route(f"/{TOKEN}/api/audit", TOKEN) == "api/audit"
    assert claudes.ui_route(f"/{TOKEN}/", TOKEN) == ""
    assert claudes.ui_route("/api/audit", TOKEN) is None
    assert claudes.ui_route("/other/api/audit", TOKEN) is None


def test_page_is_served_with_token(server):
    status, headers, body = get(server, f"/{TOKEN}/")
    assert status == 200
    assert b"<title>claudes</title>" in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"


def test_request_without_token_is_refused(server):
    status, _, body = get(server, "/api/sessions")
    assert status == 404
    assert body == b""


def test_foreign_host_is_refused(server):
    # A DNS rebinding page sends its own host name
    status, _, _ = get(server, f"/{TOKEN}/api/sessions",
                       host=f"evil.example:{server[1]}")
    assert status == 403


def test_api_returns_json(server):
    status, headers, body = get(server, f"/{TOKEN}/api/sessions")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert claudes.json.loads(body) == []


def test_unknown_route_is_refused(server):
    status, _, _ = get(server, f"/{TOKEN}/api/nothing")
    assert status == 404
