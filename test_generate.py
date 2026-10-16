"""Offline tests with a fake Archive.org. Run: python test_generate.py"""
import base64
import json
import os
import tempfile

import requests

import generate_static_catalog as G

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
BASE = "https://user.github.io/repo"
NODE = "https://ia800000.us.archive.org/12/items/fake-id/"
LIC = "https://creativecommons.org/licenses/by/4.0/"


class Resp:
    def __init__(self, status, url, ctype="application/octet-stream"):
        self.status_code, self.url, self.headers = status, url, {"Content-Type": ctype}


class FakeIA:
    def __init__(self, meta, head_fn, fetch_fn):
        self.meta, self.head_fn, self.fetch_fn = meta, head_fn, fetch_fn

    def search(self, q, max_items=500):
        return list(self.meta)

    def metadata(self, ident):
        return self.meta[ident]

    def head(self, url):
        return self.head_fn(url)

    def fetch_bytes(self, url, max_bytes=0):
        return self.fetch_fn(url)


def f(name, size=100):
    return {"name": name, "size": str(size), "source": "original", "md5": "x"}


def item_meta(server="ia800000.us.archive.org", lic=LIC):
    md = {"title": "Fake Tool v1.2", "description": "<b>A</b> tool"}
    if lic:
        md["licenseurl"] = lic
    return {"server": server, "dir": "/12/items/fake-id", "metadata": md,
            "files": [f("fake.z01"), f("fake.z02"), f("fake.zip"), f("icon0.png", 5)]}


def cfg_for(src_type="identifier", **extra):
    return G.merge_config({"sources": [{"type": src_type, "value": "fake-id", "category": "Homebrew Apps"}],
                           **extra})


def run(meta, head_fn, fetch_fn, cfg=None):
    site, cache = tempfile.mkdtemp(), tempfile.mkdtemp()
    cfg = cfg or cfg_for()
    ia = FakeIA({"fake-id": meta}, head_fn, fetch_fn)
    entries, stats = G.build(cfg, ia, site, BASE, cache)
    return cfg, site, entries, stats


def node_ok(url):
    return Resp(200, url) if url.startswith(NODE) else Resp(404, url)


def art_ok(url):
    return ("image/png", PNG) if "icon0.png" in url or "services/img" in url else None


# 1. happy path: direct node links, split set ordered, art mirrored into the site
cfg, site, entries, stats = run(item_meta(), node_ok, art_ok)
assert len(entries) == 1 and stats["errors"] == 0
e = entries[0]
assert [p["name"] for p in e["files"]] == ["fake.z01", "fake.z02", "fake.zip"]
assert all(p["url"].startswith(NODE) for p in e["files"]), e["files"]
assert e["version"] == "1.2" and e["multipart"] and e["size"] == 300
assert e["art"]["icon"].startswith(BASE + "/art/fake-id/icon")
assert e["art"]["background"].startswith(BASE + "/art/fake-id/background")  # falls back to cover art
assert os.path.exists(os.path.join(site, "art", "fake-id", "cover.png"))
G.write_site(cfg, entries, site, BASE)
src = json.load(open(os.path.join(site, "source.json")))
assert src["categories"][0]["name"] == "Homebrew Apps"
assert src["categories"][0]["items"][0]["files"][2]["url"] == NODE + "fake.zip"
assert os.path.exists(os.path.join(site, "source.gssource")) and os.path.exists(os.path.join(site, "index.html"))

# 2. item moved to another node: metadata is stale, canonical redirects to the new node
NEW = "https://ia600111.us.archive.org/5/items/fake-id/"


def moved(url):
    if url.startswith(NEW):
        return Resp(200, url)
    if url == "https://archive.org/download/fake-id/fake.z01":
        return Resp(200, NEW + "fake.z01")
    return Resp(404, url)


_, _, entries, stats = run(item_meta(), moved, art_ok)
assert entries and all(p["url"].startswith(NEW) for p in entries[0]["files"]), entries

# 3. canonical link style keeps archive.org/download URLs
canon = lambda url: Resp(200, NEW + url.rsplit("/", 1)[1])  # noqa: E731
_, _, entries, _ = run(item_meta(), canon, art_ok, cfg_for(link_style="canonical"))
assert entries[0]["files"][0]["url"] == "https://archive.org/download/fake-id/fake.z01"

# 4. art all failing -> hosted placeholders, never an empty/broken URL
_, site, entries, _ = run(item_meta(), node_ok, lambda url: None)
assert entries[0]["art"]["icon"] == BASE + "/assets/placeholder_icon.png"
assert entries[0]["art"]["background"] == BASE + "/assets/placeholder_background.png"
assert os.path.exists(os.path.join(site, "assets", "placeholder_cover.png"))

# 5. hotlink mode validates with HEAD and falls back the same way
cfg_hot = cfg_for(mirror_art=False)


def head_img(url):
    if url.startswith(NODE) and url.endswith("icon0.png"):
        return Resp(200, url, "image/png")
    return node_ok(url)


_, _, entries, _ = run(item_meta(), head_img, lambda url: None, cfg_hot)
assert entries[0]["art"]["icon"] == NODE + "icon0.png"
assert entries[0]["art"]["cover"] == BASE + "/assets/placeholder_cover.png"
assert entries[0]["art"]["background"] == BASE + "/assets/placeholder_background.png"

# 6. files genuinely gone (404) -> dropped quietly, not counted as an error
_, _, entries, stats = run(item_meta(), lambda url: Resp(404, url), art_ok)
assert entries == [] and stats["errors"] == 0 and stats["gone"] == 1


# 7. network failure -> counted as an error (so main() refuses to publish a thinned catalog)
def boom(url):
    raise requests.ConnectionError("down")


_, _, entries, stats = run(item_meta(), boom, art_ok)
assert entries == [] and stats["errors"] == 1

# 8. collection sources need a licenseurl by default; uploader sources don't
_, _, entries, _ = run(item_meta(lic=None), node_ok, art_ok, cfg_for("collection"))
assert entries == []
_, _, entries, _ = run(item_meta(lic=None), node_ok, art_ok, cfg_for("uploader"))
assert len(entries) == 1

# 9. incomplete split set is skipped
bad = item_meta()
bad["files"] = [f("fake.z01"), f("fake.z03"), f("fake.zip")]
_, _, entries, _ = run(bad, node_ok, art_ok)
assert entries == []

# 10. base URL auto-detection and config validation
os.environ["GITHUB_REPOSITORY"] = "Alice/my-repo"
assert G.detect_base_url(G.merge_config({})) == "https://alice.github.io/my-repo"
os.environ["GITHUB_REPOSITORY"] = "Alice/alice.github.io"
assert G.detect_base_url(G.merge_config({})) == "https://alice.github.io"
try:
    G.merge_config({"link_style": "bogus"})
    raise SystemExit("expected ValueError")
except ValueError:
    pass

print("generator tests OK")
