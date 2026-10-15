#!/usr/bin/env python3
"""Generate a local RSS feed snapshot from Surfit's public home/content API.

The AI news pipeline only reads RSS/Atom XML, so this bridge converts Surfit's
JSON content feed into a standards-compliant RSS 2.0 file that can be consumed
via a file:// URL.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

API_URL = "https://api.surfit.io/v1/content/my"
OUTPUT = Path(os.environ.get("OBSIDIAN_VAULT_PATH") or os.environ.get("WIKI_PATH") or str(Path.home() / "Documents/wiki")).expanduser() / "raw/rss/surfit-home-feed.xml"
USER_AGENT = "Mozilla/5.0 Hermes-Surfit-RSS-Bridge/1.0"
MAX_PAGES = 5


def fetch_page(page: int) -> dict[str, Any]:
    payload = json.dumps({"page": page, "category": None}).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def parse_pubdate(value: str | None) -> str:
    if not value:
        return email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except Exception:
            return email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return email.utils.format_datetime(parsed.astimezone(dt.timezone.utc))


def build_description(item: dict[str, Any]) -> str:
    parts = []
    author = item.get("author")
    channel = (item.get("channel") or {}).get("display_name") if isinstance(item.get("channel"), dict) else None
    content = item.get("content")
    categories = item.get("categories") or []
    if author:
        parts.append(f"작성자: {author}")
    if channel:
        parts.append(f"채널: {channel}")
    if content:
        parts.append(f"요약: {content}")
    if categories:
        cat_names = ", ".join(c.get("name", "") for c in categories if isinstance(c, dict) and c.get("name"))
        if cat_names:
            parts.append(f"분류: {cat_names}")
    return " | ".join(parts)


def main() -> int:
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    latest_pub = None

    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(page)
        batch = data.get("list") or []
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or item.get("link") or item.get("outboundUrl") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(item)
            published = item.get("publishedAt")
            if published:
                latest_pub = published
        if not data.get("hasNextPage"):
            break

    rss = ET.Element("rss", attrib={"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Surfit 홈 피드"
    ET.SubElement(channel, "link").text = "https://www.surfit.io/"
    ET.SubElement(channel, "description").text = (
        "Surfit 공개 홈 피드 JSON을 RSS 2.0으로 변환한 로컬 브리지"
    )
    ET.SubElement(channel, "language").text = "ko-KR"
    ET.SubElement(channel, "lastBuildDate").text = parse_pubdate(latest_pub)
    ET.SubElement(channel, "generator").text = "Hermes Surfit RSS Bridge"

    for item in items:
        entry = ET.SubElement(channel, "item")
        title = item.get("subject") or item.get("content") or item.get("author") or "(제목 없음)"
        link = item.get("outboundUrl") or item.get("link") or "https://www.surfit.io/"
        guid = item.get("id") or link
        ET.SubElement(entry, "title").text = str(title)
        ET.SubElement(entry, "link").text = str(link)
        ET.SubElement(entry, "guid").text = str(guid)
        ET.SubElement(entry, "pubDate").text = parse_pubdate(item.get("publishedAt"))
        ET.SubElement(entry, "description").text = build_description(item)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(OUTPUT, encoding="utf-8", xml_declaration=True)
    print(str(OUTPUT))
    print(f"items={len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
