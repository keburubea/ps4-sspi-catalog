#!/usr/bin/env python3
"""Static SSPI catalog generator for GitHub Pages.

Queries Internet Archive for the sources listed in config.yaml, groups split archives, and writes a
static site (source.json, source.gssource, index.html, mirrored art) into ./site. Package links in
source.json point DIRECTLY at Archive.org storage nodes (https://iaNNNNNN.us.archive.org/...), so
the console downloads from Archive.org itself: nothing is proxied, nothing passes through GitHub.

  python generate_static_catalog.py [--config config.yaml] [--out site]

Exit codes: 0 ok | 2 too many lookup errors (refusing to publish a damaged catalog) | 3 too few entries
"""
import argparse
import base64
import glob
import hashlib
import html
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- configuration

DEFAULTS = {
    "contact": "",                  # email/URL; goes into the User-Agent so IA can reach you
    "catalog_name": "My Homebrew Hub",
    "pages_base_url": "",           # auto-detected from GITHUB_REPOSITORY inside Actions
    "link_style": "node",           # node = ia800xxx.us.archive.org URLs | canonical = archive.org/download/...
    "mirror_art": True,             # copy covers/icons into the Pages site (most reliable)
    "categories": ["Homebrew Apps", "Emulators", "Demos", "Custom Backups"],
    "sources": [],
    "blocklist": [],
    "artwork_overrides": {},        # {identifier-or-entry-id: {icon|cover|background: url}}
    "min_interval": 0.5,            # seconds between requests to archive.org
    "max_retries": 5,
    "max_error_ratio": 0.2,         # abort (don't publish) if more than this share of lookups error out
    "min_entries": 1,
    "output_dir": "site",
    "cache_dir": ".cache",
}

KINDS = ("icon", "cover", "background")
SIZES = {"icon": (512, 512), "cover": (512, 720), "background": (1920, 1080)}
MAX_IMAGE_BYTES = 4 * 1024 * 1024
EXT_BY_TYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
GONE = (401, 403, 404, 410)
_MIN_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def log(msg):
    print(f"[gen] {msg}", flush=True)


def merge_config(user):
    cfg = dict(DEFAULTS)
    cfg.update(user or {})
    cfg["categories"] = list(cfg["categories"])
    if cfg["link_style"] not in ("node", "canonical"):
        raise ValueError("link_style must be 'node' or 'canonical'")
    for i, src in enumerate(cfg["sources"]):
        for field in ("type", "value", "category"):
            if not src.get(field):
                raise ValueError(f"sources[{i}] is missing '{field}'")
        if src["type"] not in ("identifier", "collection", "uploader", "query"):
            raise ValueError(f"sources[{i}].type must be identifier|collection|uploader|query")
        if src["category"] not in cfg["categories"]:
            cfg["categories"].append(src["category"])
    return cfg


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return merge_config(yaml.safe_load(f) or {})


def detect_base_url(cfg):
    """Public URL of the Pages site. Inside GitHub Actions it is derived automatically."""
    if cfg["pages_base_url"]:
        return cfg["pages_base_url"].rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        if name.lower() == f"{owner.lower()}.github.io":
            return f"https://{owner.lower()}.github.io"
        return f"https://{owner.lower()}.github.io/{name}"
    return ""


# --------------------------------------------------------------------------- Internet Archive client

class IA:
    def __init__(self, cfg):
        ua = "ia-sspi-pages/1.0"
        if cfg.get("contact"):
            ua += f" ({cfg['contact']})"
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": ua})
        n = cfg["max_retries"]
        retry = Retry(total=n, connect=n, read=n, status=n, backoff_factor=1.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "HEAD"]), respect_retry_after_header=True)
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        self.s.mount("https://", adapter)
        self.s.mount("http://", adapter)
        self.timeout = (10, 60)
        self.min_interval = cfg["min_interval"]
        self._lock, self._last = threading.Lock(), 0.0

    def _throttle(self):
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def get_json(self, url, params=None):
        self._throttle()
        r = self.s.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def head(self, url):
        """HEAD following redirects; response.url is the final (storage node) URL."""
        self._throttle()
        return self.s.head(url, allow_redirects=True, timeout=self.timeout)

    def fetch_bytes(self, url, max_bytes=MAX_IMAGE_BYTES):
        """-> (content_type, bytes) for a small image, or None."""
        self._throttle()
        try:
            with self.s.get(url, stream=True, timeout=self.timeout) as r:
                if r.status_code != 200:
                    return None
                ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if ctype not in EXT_BY_TYPE:
                    return None
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if len(buf) > max_bytes:
                        return None
                return ctype, bytes(buf)
        except requests.RequestException:
            return None

    def search(self, query, max_items=500, rows=200):
        out, page = [], 1
        while len(out) < max_items:
            data = self.get_json("https://archive.org/advancedsearch.php", params={
                "q": query, "fl[]": ["identifier"], "rows": rows, "page": page,
                "output": "json", "sort[]": "addeddate desc"})
            resp = data.get("response", {})
            docs = resp.get("docs", [])
            if not docs:
                break
            out.extend(d["identifier"] for d in docs)
            if len(out) >= resp.get("numFound", 0):
                break
            page += 1
        return out[:max_items]

    def metadata(self, ident):
        return self.get_json(f"https://archive.org/metadata/{quote(ident)}") or None


def resolve_source(ia, src):
    t, v = src["type"], src["value"]
    if t == "identifier":
        return [v]
    q = {"collection": f"collection:({v})", "uploader": f'uploader:"{v}"', "query": v}[t]
    if src.get("extra_query"):
        q = f"({q}) AND ({src['extra_query']})"
    return ia.search(q, max_items=int(src.get("max_items", 500)))


# --------------------------------------------------------------------------- file grouping / metadata

ARCHIVE_EXTS = {".pkg", ".zip", ".rar", ".7z"}
SKIP_SUFFIXES = ("_meta.xml", "_files.xml", "_meta.sqlite", "_reviews.xml", ".torrent", "__ia_thumb.jpg")
LAST = 10 ** 6  # plain .zip is the LAST volume of a .z01/.z02 set
_Z = re.compile(r"^(?P<base>.+)\.z(?P<n>\d{2,3})$", re.I)
_PART_RAR = re.compile(r"^(?P<base>.+)\.part(?P<n>\d+)\.rar$", re.I)
_R = re.compile(r"^(?P<base>.+)\.r(?P<n>\d{2,3})$", re.I)
_NUM = re.compile(r"^(?P<base>.+)\.(?P<ext>7z|zip|rar|pkg)\.(?P<n>\d{3})$", re.I)
CUSA_RE = re.compile(r"(CUSA\d{5})", re.I)
PKG_NAME_RE = re.compile(
    r"^(?P<reg>[A-Z]{2})\d{4}-(?P<cusa>CUSA\d{5})_\d{2}-(?P<content>[A-Z0-9]+)"
    r"(?:-A(?P<a>\d{4})-V(?P<v>\d{4}))?", re.I)
VERSION_RE = re.compile(r"(?<![A-Za-z0-9])[vV](\d+(?:\.\d+){1,3})(?![\d])")
REGIONS = {"UP": "US", "EP": "EU", "JP": "JP", "HP": "Asia", "KP": "KR", "IP": "INT"}


def as_text(v):
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v)
    return "" if v is None else str(v)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def classify(name):
    m = _Z.match(name)
    if m:
        return (m["base"] + ".zip", "zip", int(m["n"]), "z")
    m = _PART_RAR.match(name)
    if m:
        return (m["base"] + ".rar", "rar", int(m["n"]), "partrar")
    m = _R.match(name)
    if m:
        return (m["base"] + ".rar", "rar", int(m["n"]) + 1, "r")
    m = _NUM.match(name)
    if m:
        ext = m["ext"].lower()
        return (m["base"] + "." + ext, ext, int(m["n"]), "num")
    ext = os.path.splitext(name)[1].lower()
    if ext in ARCHIVE_EXTS:
        return (name, ext[1:], LAST if ext == ".zip" else 0, "single")
    return None


def _is_complete(g):
    st, orders = g["style"], [p["order"] for p in g["parts"]]
    if st == "single":
        return len(orders) == 1
    if st == "z":
        mids = [o for o in orders if o != LAST]
        return LAST in orders and mids == list(range(1, len(mids) + 1))
    if st == "r":
        mids = [o for o in orders if o != 0]
        return 0 in orders and mids == list(range(1, len(mids) + 1))
    return orders[0] in (0, 1) and orders == list(range(orders[0], orders[0] + len(orders)))


def group_files(files):
    groups = {}
    for f in files:
        name = f.get("name", "")
        if f.get("source") == "derivative" or name.startswith("__ia_"):
            continue
        if name.lower().endswith(SKIP_SUFFIXES):
            continue
        c = classify(name)
        if not c:
            continue
        key, kind, order, style = c
        g = groups.setdefault(key.lower(), {"key": key, "kind": kind, "style": "single", "parts": []})
        if style != "single":
            g["style"] = style
        g["parts"].append({"name": name, "size": _int(f.get("size")), "md5": f.get("md5"), "order": order})
    out = []
    for g in groups.values():
        g["parts"].sort(key=lambda p: (p["order"], p["name"]))
        g["complete"] = _is_complete(g)
        g["multipart"] = len(g["parts"]) > 1
        g["total_size"] = sum(p["size"] for p in g["parts"])
        out.append(g)
    return sorted(out, key=lambda g: g["key"].lower())


def display_name(g):
    return re.sub(r"(\.(zip|rar|7z|pkg))+$", "", os.path.basename(g["key"]), flags=re.I)


def describe_package(g, md, ident):
    base = os.path.basename(g["parts"][0]["name"])
    info = {"cusa": None, "region": None, "version": None}
    m = PKG_NAME_RE.match(base)
    if m:
        info["cusa"] = m["cusa"].upper()
        info["region"] = REGIONS.get(m["reg"].upper())
        if m["a"]:
            info["version"] = f"{int(m['a'][:2])}.{m['a'][2:]}"
    if not info["cusa"]:
        for text in (base, ident, as_text(md.get("title")), as_text(md.get("subject"))):
            m = CUSA_RE.search(text)
            if m:
                info["cusa"] = m.group(1).upper()
                break
    if not info["version"]:
        for text in (base, as_text(md.get("title"))):
            m = VERSION_RE.search(text)
            if m:
                info["version"] = m.group(1)
                break
    return info


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0


def clean_desc(v):
    t = re.sub(r"<[^>]+>", " ", html.unescape(as_text(v)))
    return re.sub(r"\s+", " ", t).strip()[:400]


# --------------------------------------------------------------------------- direct links

def resolve_prefix(ia, ident, meta, first, last, style):
    """Find the URL prefix (ending in '/') under which every part of a package is directly served.

    node:      https://ia800xxx.us.archive.org/NN/items/<id>/   (taken from item metadata, falling back
               to whatever node archive.org currently redirects to if the item has moved)
    canonical: https://archive.org/download/<id>/                (IA redirects the console to the node)
    Both are verified with HEAD requests (no file data is downloaded).
    -> (prefix | None, 'ok' | 'missing' | 'error')
    """
    canonical = f"https://archive.org/download/{quote(ident)}/"
    bases = []
    if style == "node":
        d = meta.get("dir")
        for host in (meta.get("server"), meta.get("d1"), meta.get("d2")):
            if host and d:
                bases.append(f"https://{host}{d}/")
    bases.append(canonical)

    prefix, saw_error = None, False
    for base in bases:
        try:
            r = ia.head(base + quote(first))
        except requests.RequestException:
            saw_error = True
            continue
        if r.status_code == 200:
            marker = f"/{ident}/"
            i = r.url.find(marker)
            host = urlparse(r.url).hostname or ""
            if style == "node" and host != "archive.org" and host.endswith(".archive.org") and i > 0:
                prefix = r.url[:i + len(marker)]  # the node that actually serves it right now
            else:
                prefix = canonical
            break
        if r.status_code not in GONE:
            saw_error = True
    if prefix is None:
        return None, ("error" if saw_error else "missing")

    if last != first:
        try:
            r = ia.head(prefix + quote(last))
        except requests.RequestException:
            return None, "error"
        if r.status_code != 200:
            return None, ("missing" if r.status_code in GONE else "error")
    return prefix, "ok"


# --------------------------------------------------------------------------- artwork

IMG = r"(?:png|jpe?g|webp)"
PATTERNS = {
    "icon": re.compile(rf"^(?:.*/)?(?:icon0|icon|logo)\.{IMG}$", re.I),
    "cover": re.compile(rf"^(?:.*/)?(?:cover|boxart|box|poster|front|pic0)\.{IMG}$", re.I),
    "background": re.compile(rf"^(?:.*/)?(?:pic1|background|bg|fanart|backdrop|banner)\.{IMG}$", re.I),
}


def safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def art_candidates(ident, files, override, prefix):
    """Ordered candidates per kind: your override > art inside the item (direct node link) > IA thumbnail."""
    found = {k: [] for k in KINDS}
    for k in KINDS:
        if override.get(k):
            found[k].append(override[k])
    for f in files:
        if f.get("source") == "derivative":
            continue
        for k, rx in PATTERNS.items():
            if rx.match(f.get("name", "")):
                found[k].append(prefix + quote(f["name"]))
    thumb = f"https://archive.org/services/img/{quote(ident)}"
    found["cover"].append(thumb)
    found["icon"].append(thumb)
    for k in KINDS:
        found[k] = list(dict.fromkeys(found[k]))
    return found


def make_placeholders(assets_dir):
    os.makedirs(assets_dir, exist_ok=True)
    for kind, (w, h) in SIZES.items():
        p = os.path.join(assets_dir, f"placeholder_{kind}.png")
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (w, h), (24, 28, 38))
            d = ImageDraw.Draw(img)
            d.rectangle([w // 20, h // 20, w - w // 20, h - h // 20],
                        outline=(70, 80, 100), width=max(2, w // 200))
            d.text((w // 2 - 24, h // 2), "NO ART", fill=(120, 130, 150))
            img.save(p)
        except Exception:
            with open(p, "wb") as f:
                f.write(_MIN_PNG)


class ArtPublisher:
    """Turns candidate URLs into one reliable URL per kind.

    mirror_art=True : download the first candidate that works into the Pages site (art/...), so the
                      console loads covers from GitHub Pages, never from a flaky hotlink.
    mirror_art=False: use the first candidate that passes a HEAD check (direct archive.org link).
    Either way, if nothing works the URL points at a placeholder image hosted on the site.
    """

    def __init__(self, ia, cfg, base_url, site, cache_dir):
        self.ia, self.base, self.site = ia, base_url, site
        self.mirror = bool(cfg["mirror_art"] and base_url)
        if cfg["mirror_art"] and not base_url:
            log("warning: no pages_base_url / GITHUB_REPOSITORY -> mirror_art disabled")
        self.cache = os.path.join(cache_dir, "art")
        os.makedirs(self.cache, exist_ok=True)
        make_placeholders(os.path.join(site, "assets"))
        self._head = {}

    def _cached_fetch(self, url):
        key = hashlib.sha1(url.encode()).hexdigest()
        for p in glob.glob(os.path.join(self.cache, key + ".*")):
            if time.time() - os.path.getmtime(p) < 7 * 86400:
                return p
        got = self.ia.fetch_bytes(url)
        if not got:
            return None
        p = os.path.join(self.cache, key + EXT_BY_TYPE[got[0]])
        with open(p, "wb") as f:
            f.write(got[1])
        return p

    def _publish_file(self, src, eid, kind):
        rel = f"art/{safe(eid)}/{kind}{os.path.splitext(src)[1]}"
        dest = os.path.join(self.site, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)
        return f"{self.base}/{quote(rel)}"

    def _head_ok(self, url):
        if url not in self._head:
            try:
                r = self.ia.head(url)
                self._head[url] = (r.status_code == 200 and
                                   r.headers.get("Content-Type", "").lower().startswith("image/"))
            except requests.RequestException:
                self._head[url] = False
        return self._head[url]

    def placeholder(self, kind):
        return f"{self.base}/assets/placeholder_{kind}.png" if self.base else ""

    def resolve(self, eid, kind, urls, cover_urls):
        pools = [urls] + ([cover_urls] if kind == "background" else [])  # background -> reuse cover
        for pool in pools:
            for u in pool:
                if self.mirror:
                    p = self._cached_fetch(u)
                    if p:
                        return self._publish_file(p, eid, kind)
                elif self._head_ok(u):
                    return u
        return self.placeholder(kind)


# --------------------------------------------------------------------------- catalog build

def build(cfg, ia, site, base_url, cache_dir):
    art = ArtPublisher(ia, cfg, base_url, site, cache_dir)
    blocked, seen, entries = set(cfg["blocklist"]), set(), []
    stats = {"items": 0, "errors": 0, "gone": 0, "fatal": False}

    for src in cfg["sources"]:
        # Open-ended sources (collection/query) must carry a license URL unless you override it.
        require_lic = src.get("require_licenseurl", src["type"] in ("collection", "query"))
        try:
            idents = resolve_source(ia, src)
        except Exception as e:
            log(f"source {src['type']}:{src['value']} failed: {e}")
            stats["fatal"] = True
            continue
        log(f"{src['type']}:{src['value']} -> {len(idents)} item(s)")

        for ident in idents:
            if ident in blocked or ident in seen:
                continue
            seen.add(ident)
            stats["items"] += 1
            try:
                meta = ia.metadata(ident)
            except Exception as e:
                log(f"  {ident}: metadata error: {e}")
                stats["errors"] += 1
                continue
            if not meta or meta.get("is_dark"):
                log(f"  {ident}: missing/dark, skipped")
                stats["gone"] += 1
                continue
            md = meta.get("metadata", {})
            lic = md.get("licenseurl")
            if require_lic and not lic:
                log(f"  {ident}: no licenseurl, skipped")
                continue
            files = meta.get("files", [])
            groups = group_files(files)
            for g in groups:
                if not g["complete"]:
                    log(f"  {ident}: incomplete split set {g['key']}, skipped")
            groups = [g for g in groups if g["complete"]]
            base_title = as_text(md.get("title")) or ident
            n = len(groups)

            for i, g in enumerate(groups):
                eid = ident if n == 1 else f"{ident}--{i + 1}"
                names = [p["name"] for p in g["parts"]]
                prefix, status = resolve_prefix(ia, ident, meta, names[0], names[-1], cfg["link_style"])
                if status != "ok":
                    log(f"  {eid}: links not verifiable ({status}), skipped")
                    stats["errors" if status == "error" else "gone"] += 1
                    continue
                info = describe_package(g, md, ident)
                override = {**cfg["artwork_overrides"].get(ident, {}), **cfg["artwork_overrides"].get(eid, {})}
                cands = art_candidates(ident, files, override, prefix)
                arts = {k: art.resolve(eid, k, cands[k], cands["cover"]) for k in KINDS}
                entries.append({
                    "id": eid, "identifier": ident,
                    "title": base_title if n == 1 else f"{base_title} - {display_name(g)}",
                    "category": src["category"],
                    "description": clean_desc(md.get("description")),
                    "cusa": info["cusa"], "region": info["region"], "version": info["version"],
                    "kind": g["kind"], "multipart": g["multipart"],
                    "size": g["total_size"], "size_text": human_size(g["total_size"]),
                    "license": lic,
                    "files": [{"name": p["name"], "url": prefix + quote(p["name"]),
                               "size": p["size"], "md5": p["md5"]} for p in g["parts"]],
                    "art": arts,
                })

    order = {c: i for i, c in enumerate(cfg["categories"])}
    entries.sort(key=lambda e: (order.get(e["category"], 99), e["title"].lower()))
    return entries, stats


# --------------------------------------------------------------------------- output

SCHEMA_VERSION = "ia-sspi-source/1"


def to_sspi(cfg, entries):
    """NOTE: exact SSPI field names are unverified. Compare site/source.json with a source file that
    works on your console and rename keys here (_item / to_sspi); nothing else depends on them."""
    def item(e):
        files = [{"name": f["name"], "url": f["url"], "size": f["size"], "md5": f.get("md5"), "part": i + 1}
                 for i, f in enumerate(e["files"])]
        return {
            "id": e["id"], "title": e["title"], "titleId": e["cusa"], "region": e["region"],
            "version": e["version"], "description": e["description"],
            "size": e["size"], "sizeText": e["size_text"],
            "icon": e["art"]["icon"], "cover": e["art"]["cover"], "background": e["art"]["background"],
            "multipart": e["multipart"], "archiveType": e["kind"], "license": e["license"],
            "files": files, "url": files[0]["url"],
        }
    cats = {}
    for e in entries:
        cats.setdefault(e["category"], []).append(item(e))
    ordered = [c for c in cfg["categories"] if c in cats] + [c for c in cats if c not in cfg["categories"]]
    return {
        "schema": SCHEMA_VERSION,
        "name": cfg["catalog_name"],
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": [{"name": c, "items": cats[c]} for c in ordered],
    }


def write_index(site, cfg, entries, base_url):
    src = f"{base_url}/source.json" if base_url else "source.json"
    rows = "".join(
        f"<li>{html.escape(e['title'])} <small>({html.escape(e['category'])}, {e['size_text']})</small></li>"
        for e in entries)
    page = (f"<!doctype html><meta charset=utf-8><title>{html.escape(cfg['catalog_name'])}</title>"
            f"<h1>{html.escape(cfg['catalog_name'])}</h1>"
            f"<p>Add this URL as a custom source in SSPI:</p><p><code>{html.escape(src)}</code></p>"
            f"<p>{len(entries)} item(s). Updated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.</p>"
            f"<ul>{rows}</ul>")
    with open(os.path.join(site, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def write_site(cfg, entries, site, base_url):
    source = to_sspi(cfg, entries)
    for name in ("source.json", "source.gssource"):
        with open(os.path.join(site, name), "w", encoding="utf-8") as f:
            json.dump(source, f, ensure_ascii=False, indent=2)
    write_index(site, cfg, entries, base_url)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default=None, help="output directory (default: output_dir from config)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    site = os.path.abspath(args.out or cfg["output_dir"])
    cache_dir = os.path.abspath(cfg["cache_dir"])
    shutil.rmtree(site, ignore_errors=True)
    os.makedirs(site)
    base_url = detect_base_url(cfg)
    log(f"site URL: {base_url or '(unknown: set pages_base_url)'}")

    entries, stats = build(cfg, IA(cfg), site, base_url, cache_dir)
    log(f"{len(entries)} entries; {stats['items']} items looked at, {stats['errors']} errors, {stats['gone']} gone")

    if stats["fatal"] or stats["errors"] > cfg["max_error_ratio"] * max(stats["items"], 1):
        log("too many lookup errors; refusing to publish a damaged catalog (previous site stays live)")
        return 2
    if len(entries) < cfg["min_entries"]:
        log("no entries produced; check the sources in config.yaml (previous site stays live)")
        return 3
    write_site(cfg, entries, site, base_url)
    log(f"wrote {site}/source.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
