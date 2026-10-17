"""Locked-down viewer for plain-HTML .sesh pages — prototype.

* The page's HTML is re-built from an allow-list (tags + a few attributes); everything else is
  dropped or turned into text.  Scripts, styles, iframes, objects, embeds, forms' actions,
  meta refresh, SVG/MathML, event handlers and javascript:/data: URLs never survive.
* No remote loads at all: images become "[image: alt]" text, CSS is dropped, links to other
  .sesh pages are routed back through the viewer, clearnet links are shown as inert text.
* Served with a CSP that allows no scripts, no connections, no images, no frames, no forms,
  plus `sandbox`; a permanent banner sits outside the page content.
"""
from __future__ import annotations

import hashlib
import html
import re
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

ALLOWED = {"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "em",
           "strong", "b", "i", "u", "s", "code", "pre", "blockquote", "a", "table", "thead",
           "tbody", "tr", "th", "td", "dl", "dt", "dd", "span", "div", "small", "sub", "sup",
           "abbr", "q", "cite", "kbd", "mark", "figure", "figcaption", "section", "article",
           "header", "footer", "nav", "main", "aside", "details", "summary", "time"}
VOID = {"br", "hr"}
DROP_WITH_CONTENT = {"script", "style", "template", "noscript", "iframe", "object", "embed",
                     "svg", "math", "frameset", "frame", "applet", "head", "title", "textarea",
                     "select", "option", "canvas", "audio", "video", "picture", "source", "xmp",
                     "noembed", "noframes", "plaintext", "base", "link", "meta"}
ATTRS = {"a": {"href", "title"}, "abbr": {"title"}, "td": {"colspan", "rowspan"},
         "th": {"colspan", "rowspan"}, "time": {"datetime"}}
SESH_RE = re.compile(r"^[a-z0-9]{52}\.sesh$|^[a-z0-9-]{1,63}\.sesh$")
BANNER = "Untyped site · {name} · not verified content · scripts and remote loads are off"


def _safe_href(href: str, page_name: str):
    """Return (viewer_href or None, display_text_suffix)."""
    h = (href or "").strip()
    if not h or re.match(r"(?i)^\s*(javascript|data|vbscript|file|blob|about):", h):
        return None, ""
    if h.startswith("#"):
        return ("#" + quote(h[1:], safe="-_")) if re.fullmatch(r"#[\w-]{1,64}", h) else None, ""
    u = urlparse(h)
    if u.scheme in ("http", "https") and u.hostname:
        if u.hostname.endswith(".sesh") and SESH_RE.match(u.hostname):
            return "/viewer?name=" + quote(u.hostname + (u.path or "/"), safe=""), ""
        return None, f" [clearnet link blocked: {u.hostname}]"
    if not u.scheme and not u.netloc:  # relative link on the same .sesh name
        path = h if h.startswith("/") else "/" + h
        return "/viewer?name=" + quote(page_name + path, safe=""), ""
    return None, ""


class _San(HTMLParser):
    def __init__(self, page_name):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.skip, self.name = [], [], 0, page_name
        self.skip_root = None
        self.pending_suffix = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip or tag in DROP_WITH_CONTENT:
            if tag not in VOID and tag not in ("base", "link", "meta", "source", "img",
                                               "input", "param", "area", "wbr", "col"):
                if not self.skip:
                    self.skip_root = tag
                self.skip += 1
            return
        if tag == "img":
            alt = dict(attrs).get("alt") or ""
            self.out.append(html.escape(f"[image: {alt[:100]}]" if alt else "[image]"))
            return
        if tag not in ALLOWED:
            return  # unknown tags dropped, their text kept
        keep = []
        suffix = ""
        for k, v in attrs:
            k = k.lower()
            if k not in ATTRS.get(tag, set()) or v is None:
                continue
            if k == "href":
                v, suffix = _safe_href(v, self.name)
                if v is None:
                    continue
            if k in ("colspan", "rowspan") and not re.fullmatch(r"\d{1,2}", v):
                continue
            keep.append(f' {k}="{html.escape(v[:500], quote=True)}"')
        if tag == "a":
            keep.append(' rel="noreferrer nofollow"')
        self.out.append(f"<{tag}{''.join(keep)}>")
        if tag not in VOID:
            self.stack.append(tag)
            self.pending_suffix.append(suffix)

    def handle_startendtag(self, tag, attrs):
        if self.skip:
            return
        if tag.lower() in DROP_WITH_CONTENT:
            return
        self.handle_starttag(tag, attrs)
        if tag.lower() in ALLOWED and tag.lower() not in VOID and self.stack and \
                self.stack[-1] == tag.lower():
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            self.skip = 0 if tag == self.skip_root else self.skip - 1
            return
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                suf = self.pending_suffix.pop()
                self.out.append(f"</{t}>")
                if suf:
                    self.out.append(html.escape(suf))
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data))

    def close(self):
        super().close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
            self.pending_suffix.pop()


def sanitize(src: str, page_name: str = "unknown.sesh") -> str:
    p = _San(page_name)
    p.feed(src[:2_000_000])
    p.close()
    return "".join(p.out)


VIEWER_CSS = ("body{margin:0;font-family:serif;background:#fff;color:#111}"
              "#srnet-banner{position:sticky;top:0;z-index:2147483647;background:#5a1a1a;color:#fff;"
              "font:14px system-ui,sans-serif;padding:6px 10px}"
              "#srnet-page{padding:10px 16px;max-width:900px;overflow-wrap:anywhere}")
CSS_HASH = "sha256-" + __import__("base64").b64encode(
    hashlib.sha256(VIEWER_CSS.encode()).digest()).decode()
CSP = ("default-src 'none'; style-src '" + CSS_HASH + "'; img-src 'none'; script-src 'none'; "
       "connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; sandbox allow-top-navigation-by-user-activation")


def render(src: str, page_name: str) -> str:
    """Full viewer page: permanent banner (outside page content) + sanitised content.
    Page CSS is never applied, so the page cannot hide or overlay the banner."""
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<style>{VIEWER_CSS}</style><title>{html.escape(page_name)} (untyped)</title></head>"
            f"<body><div id=\"srnet-banner\">{html.escape(BANNER.format(name=page_name))}</div>"
            f"<div id=\"srnet-page\">{sanitize(src, page_name)}</div></body></html>")
