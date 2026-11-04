import http.client
import ipaddress
import socket
import ssl
import time
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser


_MAX_PAGE_BYTES = 1_000_000
_MAX_PAGE_TEXT = 12_000
_MAX_CRAWL_TEXT = 60_000
_CRAWL_DELAY_SECONDS = 0.4
_SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".mp4", ".mp3",
    ".css", ".js", ".json", ".xml", ".woff", ".woff2", ".ttf", ".exe", ".dmg",
)
_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "template"}
_SECURITY_HEADERS = (
    "Strict-Transport-Security",
    "Content-Security-Policy",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "X-Frame-Options",
    "Permissions-Policy",
)


class WebAccessError(Exception):
    pass


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.title = []
        self.skip_depth = 0
        self.in_title = False
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(value for name, value in attrs if name == "href" and value)
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "tr"} and self.parts:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "tr"} and self.parts:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_title:
            self.title.append(data.strip())
        if data.strip():
            self.parts.append(data.strip())


def _resolve_public_address(hostname):
    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(result[4][0])
                for result in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
            }
        except OSError as error:
            raise WebAccessError("The requested website could not be resolved.") from error
    if not addresses or any(not address.is_global for address in addresses):
        raise WebAccessError("Only publicly routable HTTPS websites can be opened.")
    return str(sorted(addresses, key=lambda address: address.version)[0])


def _validated_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise WebAccessError("Provide a public HTTPS page URL without embedded credentials.")
    if parsed.port not in (None, 443):
        raise WebAccessError("Only standard HTTPS port 443 is supported.")
    hostname = parsed.hostname.encode("idna").decode("ascii")
    address = _resolve_public_address(hostname)
    normalized_url = urlunsplit(("https", hostname, parsed.path or "/", parsed.query, ""))
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    return hostname, address, normalized_url, target


def _request_page(url, redirects_left, max_text=_MAX_PAGE_TEXT):
    hostname, address, normalized_url, target = _validated_url(url)
    raw_socket = None
    tls_socket = None
    connection = None
    try:
        raw_socket = socket.create_connection((address, 443), timeout=12)
        tls_socket = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=hostname)
        raw_socket = None
        connection = http.client.HTTPSConnection(hostname, timeout=12, context=ssl.create_default_context())
        connection.sock = tls_socket
        tls_socket = None
        connection.request("GET", target, headers={
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
            "Accept-Encoding": "identity",
            "User-Agent": "JarvisLocalAssistant/1.0",
            "Connection": "close",
        })
        response = connection.getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            location = response.getheader("Location")
            if not location or redirects_left <= 0:
                raise WebAccessError("The page redirected too many times or had an invalid redirect.")
            return _request_page(urljoin(normalized_url, location), redirects_left - 1, max_text)
        if response.status < 200 or response.status >= 300:
            raise WebAccessError(f"The website returned HTTP {response.status}.")
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            raise WebAccessError("Only HTML and plain-text pages can be read.")
        content_length = response.getheader("Content-Length")
        if content_length and int(content_length) > _MAX_PAGE_BYTES:
            raise WebAccessError("The page is too large to read safely.")
        body = response.read(_MAX_PAGE_BYTES + 1)
        if len(body) > _MAX_PAGE_BYTES:
            raise WebAccessError("The page is too large to read safely.")
        encoding = response.headers.get_content_charset() or "utf-8"
        text = body.decode(encoding, errors="replace")
        links = []
        if content_type == "text/plain":
            title = hostname
            page_text = text
        else:
            parser = _PageText()
            parser.feed(text)
            title = " ".join(parser.title).strip() or hostname
            page_text = " ".join(" ".join(parser.parts).split())
            links = [urljoin(normalized_url, href) for href in parser.links[:2000]]
        if not page_text.strip():
            raise WebAccessError("The page did not contain readable text.")
        return {
            "title": title[:300],
            "url": normalized_url,
            "description": page_text[:max_text],
            "links": links,
            "security_headers": {
                name.lower(): response.getheader(name, "")[:2000]
                for name in _SECURITY_HEADERS
            },
        }
    except WebAccessError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException, UnicodeError, ValueError) as error:
        raise WebAccessError("The requested website could not be read securely.") from error
    finally:
        if connection:
            connection.close()
        if tls_socket:
            tls_socket.close()
        if raw_socket:
            raw_socket.close()


def read_page(url):
    return _request_page(url, redirects_left=3)


def _robots_allows(hostname):
    try:
        robots = _request_page(f"https://{hostname}/robots.txt", 1, max_text=200_000)
    except WebAccessError:
        return lambda url: True
    parser = RobotFileParser()
    parser.parse(robots["description"].splitlines())
    return lambda url: parser.can_fetch("JarvisLocalAssistant", url)


def crawl_site(start_url, max_pages=50, on_page=None, should_stop=lambda: False):
    """Read pages on one host, under the start URL's section, honoring robots.txt."""
    hostname, _, start, _ = _validated_url(start_url)
    path = urlsplit(start).path or "/"
    if path.endswith("/"):
        prefix = path
    elif "." in path.rsplit("/", 1)[-1]:
        prefix = path.rsplit("/", 1)[0] + "/"
    else:
        prefix = path + "/"
    allowed = _robots_allows(hostname)
    queue = deque([start])
    seen = {start}
    pages = failures = 0
    while queue and pages < max_pages and not should_stop():
        url = queue.popleft()
        if not allowed(url):
            continue
        try:
            page = _request_page(url, 3, max_text=_MAX_CRAWL_TEXT)
        except WebAccessError:
            failures += 1
            continue
        finally:
            time.sleep(_CRAWL_DELAY_SECONDS)
        if urlsplit(page["url"]).hostname != hostname:
            continue
        pages += 1
        if on_page:
            on_page(page)
        for link in page["links"]:
            parsed = urlsplit(link)
            if parsed.scheme != "https" or (parsed.hostname or "").lower() != hostname.lower() or parsed.port not in (None, 443):
                continue
            link_path = parsed.path or "/"
            if not (link_path.startswith(prefix) or link_path == prefix.rstrip("/")):
                continue
            if link_path.lower().endswith(_SKIP_SUFFIXES):
                continue
            clean = urlunsplit(("https", hostname, link_path, "", ""))
            if clean not in seen and len(seen) < 5000:
                seen.add(clean)
                queue.append(clean)
    return {"pages": pages, "failures": failures}