import json
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from app.core.config import settings


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def parse_json_ld(soup: BeautifulSoup) -> list[dict]:
    found = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list):
            found.extend(item for item in parsed if isinstance(item, dict))
        elif isinstance(parsed, dict):
            graph = parsed.get("@graph")
            if isinstance(graph, list):
                found.extend(item for item in graph if isinstance(item, dict))
            else:
                found.append(parsed)
    return found


def absolute_url(value: str | None, page_url: str) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value or value.startswith(("data:", "blob:", "javascript:")):
        return None
    if value.startswith("//"):
        value = "https:" + value
    # Offline ZIP exports rewrite page image URLs to ./images/...; those are archive-local
    # paths, not valid live source URLs. Keep them as source_path metadata instead of guessing.
    if value.startswith("./images/") or value.startswith("images/"):
        return None
    result = urljoin(page_url, value)
    parsed = urlparse(result)
    if parsed.scheme not in {"http", "https"}:
        return None
    return result


def extract_image(img, page_url: str, kind: str | None = None) -> dict | None:
    if img is None:
        return None
    src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or img.get("data-original")
    if not src:
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            src = srcset.split(",")[0].strip().split(" ")[0]
    raw_src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or img.get("data-original")
    if not raw_src:
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            raw_src = srcset.split(",")[0].strip().split(" ")[0]
    url = absolute_url(raw_src, page_url)
    if not url and not (raw_src and (raw_src.startswith("./images/") or raw_src.startswith("images/"))):
        return None
    return {
        "url": url,
        "source_path": raw_src if url is None else None,
        "alt": clean_text(img.get("alt")),
        "width": _int_or_none(img.get("width")),
        "height": _int_or_none(img.get("height")),
        "kind": kind,
    }


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_number(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", value.replace(",", ""))
    return float(match.group(1)) if match else None


def parse_compact_count(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([KMB])?", text, re.I)
    if not match:
        return None
    number = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    factor = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(number * factor)


def slug_from_url(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    match = re.search(r"/title/([^/]+)", path)
    return match.group(1) if match else None


def _meta(soup, *selectors):
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag:
            value = tag.get("content") or tag.get("href")
            if value:
                return value
    return None


def _labels_from_main(main) -> dict[str, str]:
    result = {}
    if main is None:
        return result
    # Site detail pages use compact label/value groups. Use nearby text nodes as a resilient fallback.
    labels = {"status", "type", "release", "author", "artist", "rating", "chapters", "bookmarks"}
    for node in main.find_all(["span", "div", "dt", "strong", "p"]):
        text = clean_text(node.get_text(" ", strip=True))
        if text and text.lower().rstrip(":") in labels:
            parent = node.parent
            if parent:
                bits = [clean_text(x.get_text(" ", strip=True)) for x in parent.find_all(["span", "div", "dd", "p"], recursive=False)]
                bits = [x for x in bits if x and x.lower().rstrip(":") != text.lower().rstrip(":")]
                if bits:
                    result[text.lower().rstrip(":")] = bits[-1]
    return result


def parse_manga_detail(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main") or soup
    json_items = parse_json_ld(soup)
    series = next((x for x in json_items if x.get("@type") in {"ComicSeries", "Book", "CreativeWorkSeries"}), {})
    title = clean_text((soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else None) or series.get("name") or _meta(soup, 'meta[property="og:title"]', 'meta[name="title"]')) or "Untitled"
    description = clean_text(series.get("description") or _meta(soup, 'meta[name="description"]', 'meta[property="og:description"]'))
    cover_url = absolute_url(
        series.get("image") or _meta(soup, 'meta[property="og:image"]', 'meta[name="twitter:image"]'),
        page_url,
    )
    images = {"cover": {"url": cover_url, "alt": title, "kind": "cover"} if cover_url else None, "banner": None}
    # The reference detail page uses a blurred backdrop image and a foreground cover image.
    for img in main.find_all("img"):
        alt = (img.get("alt") or "").strip().lower()
        classes = " ".join(img.get("class", [])).lower()
        if "backdrop" in alt or ("blur" in classes and "object-cover" in classes):
            images["banner"] = extract_image(img, page_url, "banner")
            if images["banner"]:
                break
    if images["cover"] is None:
        for img in main.find_all("img"):
            alt = (img.get("alt") or "").strip()
            if alt and alt.lower() == title.lower():
                images["cover"] = extract_image(img, page_url, "cover")
                break

    labels = _labels_from_main(main)
    authors = _split_people(series.get("author") or labels.get("author"))
    artists = _split_people(series.get("artist") or labels.get("artist"))
    genre_nodes = []
    for link in main.find_all("a"):
        text = clean_text(link.get_text(" ", strip=True))
        if text and (link.get("aria-label", "").lower().startswith("filter by") or "genre" in " ".join(link.get("class", [])).lower()):
            genre_nodes.append(text)
    if not genre_nodes:
        genre_nodes = [clean_text(x.get_text(" ", strip=True)) for x in main.select('a[href*="browse"], a[href*="genre"]')]
    if not genre_nodes and series.get("genre"):
        genre_nodes = series.get("genre") if isinstance(series.get("genre"), list) else [series.get("genre")]
    genres = list(dict.fromkeys(clean_text(str(x)) for x in genre_nodes if x and len(str(x)) < 40 and str(x).lower() not in {"more", "show more"}))

    rating = parse_number(labels.get("rating"))
    if rating is None:
        rating = parse_number(series.get("aggregateRating", {}).get("ratingValue") if isinstance(series.get("aggregateRating"), dict) else None)
    rating_count = parse_number(series.get("aggregateRating", {}).get("ratingCount") if isinstance(series.get("aggregateRating"), dict) else None)
    year = _int_or_none(series.get("datePublished") or labels.get("release"))
    chapter_items = extract_chapter_links(main, page_url, slug_from_url(page_url) or "")
    # Recommendations belong to the dedicated Related section, not every card
    # that happens to appear elsewhere on the detail page.
    recommendations = extract_related_recommendations(
        soup, page_url, exclude_slug=slug_from_url(page_url)
    )

    alt_names = series.get("alternateName") or []
    if isinstance(alt_names, str):
        alt_names = [alt_names]
    alt_names = [clean_text(x) for x in alt_names if clean_text(x)]
    return {
        "id": str(series.get("identifier")) if series.get("identifier") else None,
        "slug": slug_from_url(page_url) or "",
        "url": page_url,
        "title": {"primary": title, "alternative": list(dict.fromkeys(x for x in alt_names if x and x.lower() != title.lower())), "native": None},
        "description": description,
        "images": images,
        "metadata": {
            "type": clean_text(labels.get("type")),
            "status": clean_text(labels.get("status")),
            "release_year": year,
            "authors": authors,
            "artists": artists,
            "genres": genres,
            "rating": rating,
            "rating_count": int(rating_count) if rating_count is not None else None,
        },
        "statistics": {
            "views": None,
            "follows": None,
            "chapter_count": len(chapter_items) or int(parse_number(labels.get("chapters")) or 0) or None,
        },
        "chapters": chapter_items,
        "recommendations": recommendations,
    }


def _int_or_none(value):
    try:
        return int(str(value)[:4])
    except (TypeError, ValueError):
        return None


def _split_people(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        values = value
    elif isinstance(value, dict):
        values = [value.get("name", "")]
    else:
        values = re.split(r",|;|\|", str(value))
    out = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("name")
        if item:
            for part in re.split(r",|;|\|", str(item)):
                part = clean_text(part)
                if part and part not in out:
                    out.append(part)
    return out


def extract_chapter_links(root, page_url: str, manga_slug: str) -> list[dict]:
    chapters = []
    seen = set()
    for anchor in root.find_all("a", href=True):
        text = clean_text(anchor.get_text(" ", strip=True)) or ""
        href = anchor.get("href", "")
        match = re.search(r"(?:chapter/|/pages/)(\d+(?:\.\d+)?)", href, re.I)
        number_match = re.search(r"(?:ch(?:apter)?\\.?\\s*)(\\d+(?:\\.\\d+)?)", text, re.I)
        number = float(match.group(1)) if match else (float(number_match.group(1)) if number_match else None)
        if number is None:
            continue
        url = absolute_url(href, page_url)
        # Offline reference exports rewrite route links to ./pages/{chapter}.html.
        if url and "/pages/" in urlparse(url).path:
            url = urljoin(settings.source_base_url.rstrip("/") + "/", f"title/{manga_slug}/chapter/{number:g}")
        if not url or url in seen:
            continue
        seen.add(url)
        date_match = re.search(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}", text, re.I)
        chapters.append({
            "id": f"chapter-{number:g}",
            "slug": f"chapter-{number:g}",
            "title": f"Chapter {number:g}",
            "chapter_number": number,
            "released_at": date_match.group(0) if date_match else None,
            "href": url,
        })
    chapters.sort(key=lambda x: x["chapter_number"] if x["chapter_number"] is not None else -1, reverse=True)
    return chapters


def _extract_home_card_metadata(anchor) -> tuple[str | None, str | None, str | None]:
    """Extract the compact metadata row and type badge from a homepage manga card.

    Expected source layout:
      - metadata row: first span = chapter, second span = relative update time
      - type badge: a small overlay span such as Manhwa / Manga / Manhua
    """
    latest_chapter = None
    updated_at = None
    manga_type = None

    # The metadata row is intentionally identified by its layout classes rather than
    # its position in the card, so harmless wrapper changes do not break extraction.
    metadata_row = None
    for node in anchor.find_all(["div", "p"]):
        classes = set(node.get("class", []))
        if {"justify-between", "items-center"}.issubset(classes):
            metadata_row = node
            break

    if metadata_row is not None:
        spans = metadata_row.find_all("span", recursive=False)
        values = [clean_text(span.get_text(" ", strip=True)) for span in spans]
        values = [value for value in values if value]
        chapter_pattern = re.compile(r"^ch(?:apter)?\.?\s*\d+(?:\.\d+)?$", re.I)
        for value in values:
            if chapter_pattern.match(value):
                latest_chapter = value
                break
        if latest_chapter:
            updated_at = next((value for value in values if value != latest_chapter), None)
        elif len(values) >= 2:
            # Keep the two source slots deterministic if the chapter label is formatted
            # differently in a future source revision.
            latest_chapter, updated_at = values[0], values[1]

    # Current ManhwaHQ cards label this badge with the typography classes below.
    # The allow-list is a fallback for badge class changes and supports common formats.
    known_types = {"manhwa", "manga", "manhua", "webtoon", "comic", "novel"}
    for span in anchor.find_all("span"):
        value = clean_text(span.get_text(" ", strip=True))
        if not value:
            continue
        classes = set(span.get("class", []))
        if "tracking-wide" in classes and "leading-none" in classes:
            manga_type = value
            break
        if value.casefold() in known_types:
            manga_type = value
            break

    return latest_chapter, updated_at, manga_type


def _home_card_payload(card: dict) -> dict:
    """Keep homepage cards intentionally lean; rating/status belong to other views."""
    return {
        key: card.get(key)
        for key in ("id", "slug", "title", "cover", "latest_chapter", "updated_at", "type", "href")
    }


def _find_related_section(root):
    """Locate the source page's dedicated Related/Recommended manga section."""
    # Current ManhwaHQ markup uses <section class="mt-8 mb-2"> and an h2 "Related".
    for section in root.select("section.mt-8.mb-2"):
        heading = section.find(["h2", "h3"])
        heading_text = clean_text(heading.get_text(" ", strip=True)).casefold() if heading else ""
        if heading_text in {"related", "recommended", "recommendations", "related series"}:
            return section

    # Fallback for a future class/heading-level change while still requiring a
    # recognizable Related heading so unrelated manga grids are not included.
    for heading in root.find_all(["h2", "h3"]):
        heading_text = clean_text(heading.get_text(" ", strip=True)) or ""
        if heading_text.casefold() not in {"related", "recommended", "recommendations", "related series"}:
            continue
        section = heading.find_parent("section")
        if section is not None:
            return section
        parent = heading.parent
        for _ in range(5):
            if parent is None:
                break
            if parent.find("a", href=re.compile(r"/title/")):
                return parent
            parent = parent.parent
    return None


def extract_related_recommendations(
    root, page_url: str, exclude_slug: str | None = None
) -> list[dict]:
    """Extract recommendation cards only from the page's Related section."""
    section = _find_related_section(root)
    if section is None:
        return []

    cards = extract_manga_cards(section, page_url, exclude_slug=exclude_slug)
    return [
        {
            key: card.get(key)
            for key in (
                "id", "slug", "title", "cover", "latest_chapter",
                "updated_at", "type", "href",
            )
        }
        for card in cards
    ]


def extract_manga_cards(root, page_url: str, exclude_slug: str | None = None) -> list[dict]:
    cards = []
    seen = set()
    for anchor in root.find_all("a", href=True):
        href = anchor.get("href", "")
        # Source cards link to /title/{slug}; the archived reference uses local pages.
        local_match = re.search(r"/pages/([^/]+)\.html", href)
        slug = local_match.group(1) if local_match else slug_from_url(urljoin(page_url, href))
        if not slug and "/pages/" not in href:
            continue
        if slug and slug == exclude_slug:
            continue
        img = anchor.find("img")
        if img is None:
            continue
        heading = anchor.find(["h3", "h2"])
        title = clean_text(
            (heading.get_text(" ", strip=True) if heading else None)
            or anchor.get("title")
            or img.get("alt")
            or anchor.get_text(" ", strip=True)
        )
        if not title or len(title) > 160:
            continue
        if not slug or slug.isdigit():
            continue
        if slug in seen:
            continue
        seen.add(slug)
        cover = extract_image(img, page_url, "cover") if img else None
        if cover and not cover.get("url") and cover.get("source_path"):
            filename = cover["source_path"].rsplit("/", 1)[-1]
            document = root.find_parent("html") or root
            preload = next((link.get("href") for link in document.select('link[rel="preload"][as="image"][href]')
                            if link.get("href", "").rsplit("/", 1)[-1] == filename), None)
            if preload:
                cover["url"] = absolute_url(preload, page_url)
                cover["source_path"] = None
        latest_chapter, updated_at, manga_type = _extract_home_card_metadata(anchor)
        cards.append({
            "id": slug,
            "slug": slug,
            "title": title,
            "cover": cover,
            "latest_chapter": latest_chapter,
            "updated_at": updated_at,
            "type": manga_type,
            "rating": None,
            "status": None,
            "href": urljoin(settings.source_base_url.rstrip("/") + "/", f"title/{slug}"),
        })

    return cards


def parse_reader(html: str, page_url: str, manga_slug: str, chapter_number: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    json_items = parse_json_ld(soup)
    issue = next((x for x in json_items if x.get("@type") in {"ComicIssue", "Chapter"}), {})
    title = clean_text((soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else None) or issue.get("name")) or f"Chapter {chapter_number}"
    pages = []
    images = soup.select('img[id^="page-"]')
    if not images:
        # Fallback for reader layouts that use a reader-specific wrapper.
        images = soup.select('[data-page] img, .reader-content img, .chapter-content img')
    for position, img in enumerate(images):
        asset = extract_image(img, page_url, "chapter_page")
        if not asset:
            continue
        page_id = img.get("id", "")
        idx_match = re.search(r"(\\d+)$", page_id)
        index = int(idx_match.group(1)) if idx_match else position
        page_number = parse_number(img.get("alt")) or index + 1
        pages.append({
            "index": index,
            "page_number": int(page_number),
            "image": asset,
            "loading": img.get("loading"),
        })
    pages.sort(key=lambda x: x["index"])
    navigation = {"previous": None, "next": None, "manga": urljoin(settings.source_base_url.rstrip("/") + "/", f"title/{manga_slug}")}
    for anchor in soup.find_all("a", href=True):
        label = (anchor.get("aria-label") or anchor.get("title") or anchor.get_text(" ", strip=True)).lower()
        href = absolute_url(anchor["href"], page_url)
        if not href:
            continue
        if "previous" in label or "prev chapter" in label:
            navigation["previous"] = href
        elif "next" in label:
            navigation["next"] = href
    return {
        "manga_slug": manga_slug,
        "manga_title": clean_text(issue.get("isPartOf", {}).get("name")) if isinstance(issue.get("isPartOf"), dict) else None,
        "chapter_title": title,
        "chapter_number": parse_number(issue.get("issueNumber") or chapter_number),
        "chapter_url": page_url,
        "pages": pages,
        "page_count": len(pages),
        "navigation": navigation,
        "published_at": issue.get("datePublished"),
    }


def parse_home(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    cards = extract_manga_cards(soup, page_url)
    sections = []
    for heading in soup.find_all(["h2", "h3"]):
        title = clean_text(heading.get_text(" ", strip=True))
        if not title:
            continue
        parent = heading.parent
        for _ in range(3):
            if parent is None:
                break
            section_cards = extract_manga_cards(parent, page_url)
            if len(section_cards) >= 2:
                key = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "section"
                sections.append({"key": key, "title": title, "items": section_cards[:24]})
                break
            parent = parent.parent
    # Deduplicate sections by key and cap oversized markup.
    unique = {}
    for section in sections:
        unique.setdefault(section["key"], section)
    all_cards = cards[:60]
    section_list = list(unique.values())[:20]
    def pick_section(*needles):
        for section in section_list:
            name = section["title"].lower()
            if any(needle in name for needle in needles):
                return section["items"]
        return []
    trending = pick_section("trending")
    latest = pick_section("latest", "recent update", "new update")
    popular = pick_section("popular", "most viewed")
    recommended = pick_section("recommend", "for you")
    # Homepage cards use their own response shape: latest chapter, relative update
    # time and content type are useful to the UI; rating/status are not shown here.
    return {
        "hero": [_home_card_payload(card) for card in all_cards[:5]],
        "trending": [_home_card_payload(card) for card in trending],
        "latest_updates": [_home_card_payload(card) for card in (latest or all_cards[5:29])],
        "popular": [_home_card_payload(card) for card in popular],
        "recommended": [_home_card_payload(card) for card in recommended],
        "sections": [
            {"key": section["key"], "title": section["title"],
             "items": [_home_card_payload(card) for card in section["items"]]}
            for section in section_list
        ],
    }


def parse_browse(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main") or soup
    items = extract_manga_cards(main, page_url)
    genres = []
    for link in main.find_all("a", href=True):
        text = clean_text(link.get_text(" ", strip=True))
        if text and len(text) < 40 and ("genre" in link.get("href", "").lower() or link.get("aria-label", "").lower().startswith("filter by")):
            genres.append(text)
    return {
        "items": items,
        "available_genres": list(dict.fromkeys(genres)),
        "sort_options": [],
        "applied_filters": {},
    }


# Normalizers for the public JSON data endpoint referenced by ManhwaHQ's own frontend bundle.
def normalize_source_image(value, kind: str | None = None, alt: str | None = None):
    if not value:
        return None
    if isinstance(value, dict):
        raw = (value.get("url") or value.get("src") or value.get("image") or value.get("srcUrl")
               or value.get("imageUrl") or value.get("image_url") or value.get("src_url")
               or value.get("original") or value.get("path") or value.get("file"))
        alt = value.get("alt") or alt
    else:
        raw = str(value)
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    if raw.startswith("/"):
        raw = urljoin(settings.source_data_api_base_url.rstrip("/") + "/", raw.lstrip("/"))
    elif not raw.startswith(("http://", "https://")):
        raw = urljoin(settings.source_data_api_base_url.rstrip("/") + "/", raw)
    if raw.startswith("http://"):
        raw = "https://" + raw[len("http://"):]
    return {"url": raw, "source_path": None, "alt": clean_text(alt), "kind": kind}


def normalize_source_card(item: dict) -> dict:
    title = clean_text(item.get("title") or item.get("name") or item.get("manga_title")) or "Untitled"
    slug = str(item.get("slug") or item.get("id") or title.lower().replace(" ", "-")).strip()
    cover = normalize_source_image(
        item.get("coverCard") or item.get("cover_card") or item.get("coverImage") or
        item.get("cover_url") or item.get("cover") or item.get("thumbnail") or item.get("image"),
        "cover", title,
    )
    latest = item.get("latestChapter") or item.get("latest_chapter") or item.get("chapter")
    return {
        "id": str(item.get("id") or slug),
        "slug": slug,
        "title": title,
        "cover": cover,
        "latest_chapter": str(latest) if latest is not None else None,
        "updated_at": item.get("updatedAt") or item.get("updated_at") or item.get("date"),
        "rating": parse_number(str(item.get("score") or item.get("rating") or "")),
        "status": clean_text(item.get("status")),
        "href": urljoin(settings.source_base_url.rstrip("/") + "/", f"title/{slug}"),
    }


def normalize_source_detail(item: dict, page_url: str) -> dict:
    title = clean_text(item.get("title") or item.get("name")) or "Untitled"
    slug = str(item.get("slug") or slug_from_url(page_url) or item.get("id") or "")
    alternative = item.get("alternative") or item.get("alternativeTitle") or []
    if isinstance(alternative, str):
        alternative = [x.strip() for x in re.split(r"[,;|]", alternative) if x.strip()]
    if not isinstance(alternative, list):
        alternative = []
    genres = item.get("genres") or item.get("genre") or item.get("categories") or []
    if isinstance(genres, str):
        genres = [x.strip() for x in re.split(r"[,|/]", genres) if x.strip()]
    chapters_raw = item.get("chapters_list") or item.get("chapter_list") or []
    chapters = []
    for index, chapter in enumerate(chapters_raw if isinstance(chapters_raw, list) else []):
        if not isinstance(chapter, dict):
            continue
        number = chapter.get("chapterNumber", chapter.get("chapter", chapter.get("num", index)))
        try:
            number_value = float(number)
        except (TypeError, ValueError):
            number_value = None
        chapter_slug = str(chapter.get("slug") or chapter.get("id") or f"chapter-{number}")
        chapter_href = urljoin(settings.source_base_url.rstrip("/") + "/", f"title/{slug}/chapter/{number}")
        chapters.append({
            "id": str(chapter.get("id") or chapter_slug),
            "slug": chapter_slug,
            "title": clean_text(chapter.get("title") or chapter.get("name")) or f"Chapter {number}",
            "chapter_number": number_value,
            "released_at": chapter.get("date") or chapter.get("releasedAt"),
            "href": chapter_href,
        })
    chapters.sort(key=lambda x: x["chapter_number"] if x["chapter_number"] is not None else -1, reverse=True)
    cover = normalize_source_image(item.get("coverImage") or item.get("cover_url") or item.get("cover"), "cover", title)
    banner = normalize_source_image(item.get("bannerImage") or item.get("banner_image") or item.get("banner"), "banner", title)
    card_cover = normalize_source_image(item.get("coverCard") or item.get("cover_card"), "card", title)
    authors = _split_people(item.get("author") or item.get("authors"))
    artists = _split_people(item.get("artist") or item.get("artists"))
    score = parse_number(str(item.get("score") or item.get("rating") or ""))
    if score is not None and 0 < score <= 5:
        score = round(score * 2, 1)
    return {
        "id": str(item.get("id") or slug),
        "slug": slug,
        "url": page_url,
        "title": {"primary": title, "alternative": alternative, "native": item.get("native")},
        "description": clean_text(item.get("synopsis") or item.get("description")),
        "images": {"cover": cover or card_cover, "banner": banner},
        "metadata": {
            "type": item.get("type") or item.get("format"),
            "status": item.get("status"),
            "release_year": _int_or_none(item.get("releaseYear") or item.get("release_year") or item.get("year")),
            "authors": authors,
            "artists": artists,
            "genres": [clean_text(str(x.get("name") if isinstance(x, dict) else x)) for x in genres if x],
            "rating": score,
            "rating_count": _int_or_none(item.get("ratingCount") or item.get("rating_count")),
        },
        "statistics": {
            "views": parse_compact_count(item.get("popularity") or item.get("views")),
            "follows": parse_compact_count(item.get("bookmarkCount") or item.get("bookmark_count") or item.get("bookmarks")),
            "chapter_count": len(chapters) or _int_or_none(item.get("chapters") or item.get("chapter_count")),
        },
        "chapters": chapters,
        "recommendations": [],
    }


def normalize_source_reader(item: dict, page_url: str, manga_slug: str, chapter_number: str) -> dict:
    images = item.get("images") or item.get("pages") or item.get("chapter_images") or []
    pages = []
    for position, image_value in enumerate(images if isinstance(images, list) else []):
        asset = normalize_source_image(image_value, "chapter_page", f"Page {position + 1}")
        if not asset:
            continue
        if isinstance(image_value, dict):
            page_no = image_value.get("page") or image_value.get("pageNumber") or image_value.get("number") or position + 1
        else:
            page_no = position + 1
        try:
            page_no = int(page_no)
        except (TypeError, ValueError):
            page_no = position + 1
        pages.append({"index": position, "page_number": page_no, "image": asset, "loading": None})
    return {
        "manga_slug": manga_slug,
        "manga_title": clean_text(item.get("mangaTitle") or item.get("manga_title") or item.get("seriesTitle")),
        "chapter_title": clean_text(item.get("title") or item.get("name")) or f"Chapter {chapter_number}",
        "chapter_number": parse_number(str(item.get("chapterNumber") or item.get("chapter") or chapter_number)),
        "chapter_url": page_url,
        "pages": pages,
        "page_count": len(pages),
        "navigation": {
            "previous": item.get("previous") or item.get("prevChapter"),
            "next": item.get("next") or item.get("nextChapter"),
            "manga": urljoin(settings.source_base_url.rstrip("/") + "/", f"title/{manga_slug}"),
        },
        "published_at": item.get("date") or item.get("releasedAt"),
    }
