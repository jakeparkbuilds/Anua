"""Web pack: truths about a URL / page that ANY agent making claims about a web page
can be graded against. httpx + stdlib html.parser only.

Targets are URLs; a bare domain is normalised to https://<domain>/. One GET per URL per
run (cached), plus one each for robots.txt / sitemap.xml / llms.txt at the origin.

Verdict vs unavailable: a certificate verification failure is a verdict for
`web.fetchable` (False). For every other claim the page content is required, so a page
we could not fetch at all raises OracleUnavailable.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlsplit, urlunsplit
import httpx

from ..errors import OracleUnavailable, is_cert_verification_error
from . import spec

_UA = "ans-bench/0.2 (+https://anuabot.vip)"
_TIMEOUT = 15


def normalize_url(target: str) -> str:
    t = target.strip()
    if not re.match(r"^https?://", t, re.I):
        t = f"https://{t}/"
    return t


def _origin(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "", "", ""))


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self.meta: dict[str, str] = {}      # name/property (lower) -> content
        self.canonical: Optional[str] = None
        self.h1 = 0
        self.jsonld = 0
        self._in_script = None
        self.lang: Optional[str] = None
        self.text_chars = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"].strip().lower()
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").strip().lower()
            if key:
                self.meta.setdefault(key, a.get("content", "").strip())
            if a.get("charset"):
                self.meta.setdefault("charset", a["charset"].strip().lower())
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonical = self.canonical or a.get("href", "").strip()
        elif tag == "h1":
            self.h1 += 1
        elif tag == "script":
            self._in_script = a.get("type", "").strip().lower()
            if self._in_script == "application/ld+json":
                self.jsonld += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_script = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_script is None:
            self.text_chars += len(data.strip())


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    redirects: int
    content_type: str
    charset: Optional[str]
    parsed: _Parser
    headers: dict = field(default_factory=dict)


def _get(url: str, verify: bool = True) -> httpx.Response:
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, verify=verify,
                          headers={"User-Agent": _UA, "Accept": "text/html,*/*;q=0.8"}) as c:
            return c.get(url)
    except httpx.HTTPError as e:
        if is_cert_verification_error(e):
            raise
        raise OracleUnavailable(f"GET {url}: {type(e).__name__}: {e}") from e


@lru_cache(maxsize=256)
def _page(target: str) -> Page:
    url = normalize_url(target)
    r = _get(url)                                  # cert failure propagates (see fetchable)
    p = _Parser()
    ct = r.headers.get("content-type", "")
    if "html" in ct or "xml" in ct or not ct:
        try:
            p.feed(r.text)
        except Exception:                          # malformed markup: keep what we got
            pass
    charset = r.charset_encoding or p.meta.get("charset")
    return Page(url, str(r.url), r.status_code, len(r.history), ct,
                charset.lower() if charset else None, p, dict(r.headers))


def _page_or_unavailable(target: str) -> Page:
    try:
        return _page(target)
    except httpx.HTTPError as e:                   # only cert-verification errors reach here
        raise OracleUnavailable(f"GET {target}: certificate verification failed; page not fetched") from e


@lru_cache(maxsize=256)
def _aux(origin: str, path: str) -> Optional[str]:
    """Body of origin+path if it exists with a 2xx and looks like text, else None."""
    r = _get(f"{origin}{path}", verify=False)
    if r.status_code // 100 != 2:
        return None
    ct = r.headers.get("content-type", "").lower()
    if "html" in ct and path != "/sitemap.xml":    # SPA catch-all serving index.html for /robots.txt
        return None
    return r.text


# ---- oracles ----------------------------------------------------------------
def fetchable(target: str) -> bool:
    try:
        return _page(target).status < 400
    except httpx.HTTPError:
        return False                               # cert failure: page is NOT fetchable


# A protocol endpoint is not a document. dnsdoc's MCP endpoint answers our HTML GET with
# 406 Not Acceptable because streamable-HTTP wants Accept: application/json, text/event-stream
# — it is plainly there and plainly serving. Grading "publishes an MCP endpoint" with
# `fetchable` called that a broken claim. 404 and 5xx still mean nothing is there.
_SERVING_BUT_NOT_FOR_US = frozenset({401, 402, 403, 405, 406, 415, 422, 429})


def endpoint_live(target: str) -> bool:
    """True iff SOMETHING is serving at this URL — including an endpoint that rejects the
    shape of our request. Use this for a claim that an endpoint exists; use `fetchable`
    when the claim is that a document can actually be retrieved."""
    try:
        st = _page(target).status
    except httpx.HTTPError:
        return False                               # cert failure: nothing usable is there
    return st < 400 or st in _SERVING_BUT_NOT_FOR_US


def status(target: str) -> int:                 return _page_or_unavailable(target).status
def redirect_count(target: str) -> int:         return _page_or_unavailable(target).redirects
def final_url(target: str) -> str:              return _page_or_unavailable(target).final_url
def title(target: str) -> str:                  return " ".join(_page_or_unavailable(target).parsed.title.split())
def title_present(target: str) -> bool:         return bool(title(target))
def meta_description(target: str) -> str:       return _page_or_unavailable(target).parsed.meta.get("description", "")
def meta_description_present(target: str) -> bool: return bool(meta_description(target))
def h1_count(target: str) -> int:               return _page_or_unavailable(target).parsed.h1
def h1_present(target: str) -> bool:            return h1_count(target) > 0
def canonical(target: str) -> str:              return _page_or_unavailable(target).parsed.canonical or ""
def canonical_present(target: str) -> bool:     return bool(canonical(target))
def jsonld_present(target: str) -> bool:        return _page_or_unavailable(target).parsed.jsonld > 0
def viewport_present(target: str) -> bool:      return "viewport" in _page_or_unavailable(target).parsed.meta
def lang(target: str) -> str:                   return _page_or_unavailable(target).parsed.lang or ""
def charset(target: str) -> str:                return _page_or_unavailable(target).charset or ""
def og_present(target: str) -> bool:
    m = _page_or_unavailable(target).parsed.meta
    return any(k.startswith("og:") for k in m)
def text_chars(target: str) -> int:             return _page_or_unavailable(target).parsed.text_chars
def https_redirect(target: str) -> bool:
    """True iff http://host/ ends up on an https:// URL."""
    host = urlsplit(normalize_url(target)).netloc
    r = _get(f"http://{host}/", verify=False)
    return str(r.url).lower().startswith("https://")


def robots_txt_exists(target: str) -> bool:
    return _aux(_origin(normalize_url(target)), "/robots.txt") is not None


def robots_disallow_all(target: str) -> bool:
    """True iff robots.txt has a `Disallow: /` under `User-agent: *`."""
    body = _aux(_origin(normalize_url(target)), "/robots.txt")
    if body is None:
        return False
    star = False
    for line in body.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            star = (v == "*")
        elif k == "disallow" and star and v == "/":
            return True
    return False


def sitemap_exists(target: str) -> bool:
    origin = _origin(normalize_url(target))
    body = _aux(origin, "/sitemap.xml")
    if body is not None and "<" in body:
        return True
    robots = _aux(origin, "/robots.txt") or ""
    return any(l.strip().lower().startswith("sitemap:") for l in robots.splitlines())


def llms_txt_exists(target: str) -> bool:
    return _aux(_origin(normalize_url(target)), "/llms.txt") is not None


P = "web"
_NOPAGE = ("https://example.com/this-page-does-not-exist",)
_BARE = ("https://example.com/",)     # has title+h1+viewport; NO meta description, canonical, JSON-LD, OG, robots, sitemap
SPECS = {s.key: s for s in [
    spec("web.fetchable", fetchable, "True iff GET of the URL (following redirects, verifying TLS) ends in a status < 400",
         input="url", negatives=_NOPAGE + ("https://expired.badssl.com/",), pack=P),
    spec("web.endpoint_live", endpoint_live,
         "True iff something is serving at this URL, including a protocol endpoint that "
         "rejects our request shape (405/406/415) or demands payment or auth (401/402/403). "
         "404, 5xx and TLS failures are False. Use for 'exposes an endpoint at X' claims.",
         input="url", negatives=_NOPAGE, pack=P),
    spec("web.status", status, "Final HTTP status code after following redirects", input="url",
         comparator="eq", returns="int", negatives=_NOPAGE, pack=P),
    spec("web.redirect_count", redirect_count, "Number of redirects followed to reach the final URL", input="url",
         comparator="eq", returns="int", pack=P),
    spec("web.final_url", final_url, "The URL the request ended on after redirects", input="url",
         comparator="contains", returns="str", pack=P),
    spec("web.title", title, "Text of the <title> element, whitespace-collapsed ('' if none)", input="url",
         comparator="contains", returns="str", pack=P),
    spec("web.title_present", title_present, "True iff the page has a non-empty <title>", input="url", pack=P),
    spec("web.meta_description", meta_description, "Content of <meta name=description> ('' if none)", input="url",
         comparator="contains", returns="str", negatives=_BARE, pack=P),
    spec("web.meta_description_present", meta_description_present, "True iff <meta name=description> is present and non-empty",
         input="url", negatives=_BARE, pack=P),
    spec("web.h1_count", h1_count, "Number of <h1> elements on the page", input="url",
         comparator="eq", returns="int", pack=P),
    spec("web.h1_present", h1_present, "True iff the page has at least one <h1>", input="url", pack=P),
    spec("web.canonical", canonical, "href of <link rel=canonical> ('' if none)", input="url",
         comparator="contains", returns="str", negatives=_BARE, pack=P),
    spec("web.canonical_present", canonical_present, "True iff a <link rel=canonical> is present", input="url",
         negatives=_BARE, pack=P),
    spec("web.jsonld_present", jsonld_present, "True iff at least one <script type=application/ld+json> (structured data) is present",
         input="url", negatives=_BARE, pack=P),
    spec("web.og_present", og_present, "True iff any Open Graph <meta property=og:*> tag is present", input="url",
         negatives=_BARE, pack=P),
    spec("web.viewport_present", viewport_present, "True iff <meta name=viewport> is present (mobile-friendly signal)",
         input="url", pack=P),
    spec("web.lang", lang, "Value of <html lang> lowercased ('' if none)", input="url",
         comparator="contains", returns="str", pack=P),
    spec("web.charset", charset, "Response charset (from Content-Type or <meta charset>), lowercased", input="url",
         comparator="contains", returns="str", pack=P),
    spec("web.text_chars", text_chars, "Approximate count of visible text characters outside <script> (very low means JS-rendered)",
         input="url", comparator="eq", returns="int", pack=P),
    spec("web.https_redirect", https_redirect, "True iff http://host/ redirects to an https:// URL", input="url", pack=P),
    spec("web.robots_txt_exists", robots_txt_exists, "True iff /robots.txt exists at the origin (2xx, non-HTML)",
         input="url", negatives=_BARE, pack=P),
    spec("web.robots_disallow_all", robots_disallow_all, "True iff robots.txt disallows / for User-agent: *",
         input="url", pack=P),
    spec("web.sitemap_exists", sitemap_exists, "True iff /sitemap.xml exists or robots.txt declares a Sitemap:",
         input="url", negatives=_BARE, pack=P),
    spec("web.llms_txt_exists", llms_txt_exists, "True iff /llms.txt exists at the origin", input="url",
         negatives=_BARE, pack=P),
]}
