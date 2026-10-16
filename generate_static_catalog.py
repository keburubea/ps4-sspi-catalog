#!/usr/bin/env python3
"""
generate_static_catalog.py

Queries the public Internet Archive APIs, finds `.pkg` files (and split
archives such as `.z01`, `.part1.rar`, `.7z.001`), and writes a static
`source.gssource` catalog (JSON) that links straight to archive.org.

Root object:
    {
      "name": "...",
      "icon": "...",
      "categories": ["Games", "Homebrew Apps"],
      "packages": [ ... ]
    }

Usage:
    python generate_static_catalog.py
    python generate_static_catalog.py --config config.yaml --output source.gssource

The script always exits with status 0 so CI/CD jobs (e.g. GitHub Actions)
finish even when some items fail or produce no results. Problems are logged.
"""

import argparse
import html
import json
import logging
import os
import re
import sys
import tempfile
import time
from urllib.parse import quote

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("catalog")

DEFAULT_CONFIG = {
    "catalog": {
        "name": "My Custom Catalog",
        "icon": "https://archive.org/images/glogo.jpg",
        "categories": ["Games", "Homebrew Apps"],
        "default_category": "",  # empty -> first entry of `categories`
        "output_path": "source.gssource",
        "fallback_image": "https://archive.org/images/glogo.jpg",
    },
    "archive": {
        "search_url": "https://archive.org/advancedsearch.php",
        "metadata_url": "https://archive.org/metadata",
        "download_base": "https://archive.org/download",
        "thumbnail_base": "https://archive.org/services/img",
        "rows_per_page": 100,
        "request_timeout": 30,
        "retries": 4,
        "backoff_factor": 1.5,
        "delay_between_requests": 0.5,
        "user_agent": "static-catalog-generator/1.0",
    },
    "file_patterns": [
        r"\.pkg$",
        r"\.z\d{2,}$",
        r"\.part0*\d+\.rar$",
        r"\.7z\.\d{3}$",
    ],
    # internal key -> output key name in each package object.
    # Set a value to null to omit that field.
    "package_fields": {
        "id": "id",
        "identifier": "identifier",
        "title": "title",
        "description": "description",
        "category": "category",
        "icon": "icon",
        "size": "size",
        "downloadURL": "url",
        "filename": "filename",
        "type": "type",
        "parts": "parts",
        "itemURL": "itemURL",
    },
    "max_description_length": 600,
    "queries": [],
    "items": [],
}

SPLIT_SUFFIX = re.compile(
    r"(\.z\d{2,}|\.part0*\d+\.rar|\.7z\.\d{3})$", re.IGNORECASE
)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def deep_merge(base, override):
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user_cfg = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        log.warning("Config %s not found; using defaults.", path)
        user_cfg = {}
    except Exception as exc:
        log.error("Could not read config %s: %s; using defaults.", path, exc)
        user_cfg = {}
    return deep_merge(DEFAULT_CONFIG, user_cfg)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def build_session(cfg):
    session = requests.Session()
    retry = Retry(
        total=cfg["retries"],
        connect=cfg["retries"],
        read=cfg["retries"],
        status=cfg["retries"],
        backoff_factor=cfg["backoff_factor"],
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": cfg["user_agent"]})
    return session


def polite_sleep(cfg):
    delay = cfg.get("delay_between_requests", 0)
    if delay:
        time.sleep(delay)


# --------------------------------------------------------------------------- #
# Archive.org access
# --------------------------------------------------------------------------- #
def search_identifiers(session, cfg, query, max_items):
    """Run an Archive.org advanced search; return a list of identifiers."""
    identifiers = []
    page = 1
    rows = max(1, min(int(cfg["rows_per_page"]), 1000))

    while len(identifiers) < max_items:
        params = {
            "q": query,
            "fl[]": "identifier",
            "rows": min(rows, max_items - len(identifiers)),
            "page": page,
            "output": "json",
        }
        try:
            resp = session.get(
                cfg["search_url"], params=params, timeout=cfg["request_timeout"]
            )
            resp.raise_for_status()
            docs = resp.json().get("response", {}).get("docs", [])
        except Exception as exc:
            log.error("Search failed (query=%r, page=%d): %s", query, page, exc)
            break

        if not docs:
            break
        for doc in docs:
            ident = doc.get("identifier")
            if ident:
                identifiers.append(ident)
        if len(docs) < params["rows"]:
            break
        page += 1
        polite_sleep(cfg)

    return identifiers[:max_items]


def fetch_item_metadata(session, cfg, identifier):
    url = f"{cfg['metadata_url']}/{quote(identifier, safe='')}"
    resp = session.get(url, timeout=cfg["request_timeout"])
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def first_value(value, default=""):
    """Archive.org fields may be a string or a list of strings."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else default
    return default if value is None else value


def clean_text(value, limit):
    text = first_value(value, "")
    if not isinstance(text, str):
        text = str(text)
    text = html.unescape(TAG_RE.sub(" ", text.replace("<br>", "\n")))
    text = WS_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def canonical_url(cfg, identifier, filename):
    return (
        f"{cfg['download_base']}/{quote(identifier, safe='')}/"
        f"{quote(filename, safe='/')}"
    )


def has_artwork(files):
    """True if the item ships its own thumbnail/tile."""
    for f in files:
        name = str(f.get("name", "")).lower()
        fmt = str(f.get("format", "")).lower()
        if name == "__ia_thumb.jpg" or fmt in ("item tile", "thumbnail"):
            return True
    return False


def extract_files(cfg, identifier, files, patterns):
    """Return list of file groups: single .pkg files or grouped split archives."""
    singles = []
    split_groups = {}

    for f in files:
        try:
            name = f.get("name")
            if not name or f.get("private") in (True, "true", "True"):
                continue
            if not any(p.search(name) for p in patterns):
                continue

            size = to_int(f.get("size"))
            entry = {
                "filename": name,
                "size": size,
                "downloadURL": canonical_url(cfg, identifier, name),
            }

            if SPLIT_SUFFIX.search(name):
                base = SPLIT_SUFFIX.sub("", name)
                split_groups.setdefault(base, []).append(entry)
            else:
                entry["type"] = "pkg"
                singles.append(entry)
        except Exception as exc:  # one bad file entry must not stop the item
            log.warning("[%s] skipped file entry: %s", identifier, exc)

    groups = sorted(singles, key=lambda e: e["filename"].lower())

    for base, parts in sorted(split_groups.items()):
        parts.sort(key=lambda e: e["filename"].lower())
        groups.append(
            {
                "type": "split-archive",
                "filename": base,
                "size": sum(p["size"] for p in parts),
                "downloadURL": parts[0]["downloadURL"],
                "parts": parts,
            }
        )
    return groups


def map_fields(internal, field_map):
    """Rename/filter internal keys into the configured output keys."""
    out = {}
    for key, out_key in field_map.items():
        if not out_key:
            continue
        value = internal.get(key)
        if value in (None, "", []):
            continue
        out[out_key] = value
    return out


def build_package_entries(cfg, identifier, data, patterns, category):
    """Convert one Archive.org item into zero or more package objects."""
    if not data or "metadata" not in data:
        log.info("[%s] no metadata (missing, dark or excluded); skipping.", identifier)
        return []

    meta = data.get("metadata") or {}
    files = data.get("files") or []

    groups = extract_files(cfg["archive"], identifier, files, patterns)
    if not groups:
        log.info("[%s] no matching package files; skipping.", identifier)
        return []

    arch = cfg["archive"]
    title = clean_text(meta.get("title"), 200) or identifier
    description = clean_text(meta.get("description"), cfg["max_description_length"])

    if has_artwork(files):
        icon = f"{arch['thumbnail_base']}/{quote(identifier, safe='')}"
    else:
        icon = cfg["catalog"]["fallback_image"]

    multiple = len(groups) > 1
    packages = []
    for group in groups:
        try:
            internal = {
                "id": f"{identifier}:{group['filename']}" if multiple else identifier,
                "identifier": identifier,
                "title": f"{title} ({group['filename']})" if multiple else title,
                "description": description,
                "category": category,
                "icon": icon,
                "size": group["size"],
                "downloadURL": group["downloadURL"],
                "filename": group["filename"],
                "type": group["type"],
                "parts": [
                    {
                        "filename": p["filename"],
                        "size": p["size"],
                        "url": p["downloadURL"],
                    }
                    for p in group.get("parts", [])
                ],
                "itemURL": f"https://archive.org/details/{quote(identifier, safe='')}",
            }
            packages.append(map_fields(internal, cfg["package_fields"]))
        except Exception as exc:
            log.warning("[%s] could not build package %r: %s", identifier, group, exc)
    return packages


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_json_atomic(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(args):
    config = load_config(args.config)
    arch = config["archive"]
    cat = config["catalog"]
    output_path = args.output or cat["output_path"]

    categories = [str(c) for c in (cat.get("categories") or [])]
    default_category = cat.get("default_category") or (
        categories[0] if categories else "Uncategorized"
    )

    patterns = []
    for raw in config["file_patterns"]:
        try:
            patterns.append(re.compile(raw, re.IGNORECASE))
        except re.error as exc:
            log.error("Invalid file pattern %r: %s", raw, exc)
    if not patterns:
        patterns = [re.compile(r"\.pkg$", re.IGNORECASE)]

    session = build_session(arch)

    # (identifier -> category), de-duplicated, first mention wins.
    ordered = {}

    def add(ident, category):
        ident = (ident or "").strip()
        if ident and ident not in ordered:
            ordered[ident] = category or default_category

    for entry in config.get("items") or []:
        try:
            if isinstance(entry, dict):
                add(entry.get("id") or entry.get("identifier"), entry.get("category"))
            else:
                add(str(entry), None)
        except Exception as exc:
            log.error("Bad item entry %r: %s", entry, exc)

    for q in config.get("queries") or []:
        try:
            is_map = isinstance(q, dict)
            query = q["query"] if is_map else str(q)
            label = q.get("name", query) if is_map else query
            limit = to_int(q.get("max_items"), 100) if is_map else 100
            category = q.get("category") if is_map else None
            log.info("Searching %r (max %d)", label, limit)
            for ident in search_identifiers(session, arch, query, limit):
                add(ident, category)
            polite_sleep(arch)
        except Exception as exc:
            log.error("Query %r failed: %s", q, exc)

    log.info("Inspecting %d unique item(s).", len(ordered))

    packages = []
    skipped = 0
    for ident, category in ordered.items():
        try:
            data = fetch_item_metadata(session, arch, ident)
            built = build_package_entries(config, ident, data, patterns, category)
            if built:
                packages.extend(built)
            else:
                skipped += 1
        except Exception as exc:
            skipped += 1
            log.error("[%s] failed: %s", ident, exc)
        polite_sleep(arch)

    title_key = config["package_fields"].get("title") or "title"
    packages.sort(key=lambda p: str(p.get(title_key, "")).lower())

    # Keep the root category list consistent with categories actually used.
    cat_key = config["package_fields"].get("category")
    if cat_key:
        for p in packages:
            used = p.get(cat_key)
            if used and used not in categories:
                categories.append(used)

    catalog = {
        "name": cat["name"],
        "icon": cat["icon"],
        "categories": categories,
        "packages": packages,
    }
    write_json_atomic(output_path, catalog)
    log.info(
        "Wrote %s with %d package(s); %d item(s) skipped.",
        output_path,
        len(packages),
        skipped,
    )


def main():
    parser = argparse.ArgumentParser(description="Generate a static source.gssource.")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument(
        "--output", default=None, help="Override output path (e.g. source.json)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        run(args)
    except Exception as exc:  # never fail the CI job
        log.exception("Unexpected error: %s", exc)
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
