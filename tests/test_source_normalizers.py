from app.scraper.parsers import normalize_source_detail, normalize_source_reader, normalize_source_card, parse_compact_count


def test_source_detail_normalizer_maps_source_fields():
    raw = {
        "id": 7, "slug": "demo-series", "title": "Demo Series",
        "synopsis": "Description", "coverImage": "/uploads/covers/demo.webp",
        "genres": ["Action", "Fantasy"], "chapters_list": [
            {"chapterNumber": 2, "title": "Chapter 2"},
            {"chapterNumber": 1, "title": "Chapter 1"},
        ], "author": "Writer A, Writer B", "score": 4.5,
    }
    result = normalize_source_detail(raw, "https://manhwahq.com/title/demo-series")
    assert result["slug"] == "demo-series"
    assert result["images"]["cover"]["url"] == "https://b.toonking.xyz/uploads/covers/demo.webp"
    assert result["metadata"]["rating"] == 9.0
    assert result["metadata"]["authors"] == ["Writer A", "Writer B"]
    assert [chapter["chapter_number"] for chapter in result["chapters"]] == [2.0, 1.0]


def test_source_reader_normalizer_preserves_page_order():
    result = normalize_source_reader(
        {"images": ["/uploads/chapter/01.jpg", "https://cdn.example/02.jpg"]},
        "https://manhwahq.com/title/demo-series/chapter/1", "demo-series", "1",
    )
    assert result["page_count"] == 2
    assert result["pages"][0]["page_number"] == 1
    assert result["pages"][1]["image"]["url"] == "https://cdn.example/02.jpg"


def test_source_card_normalizer():
    result = normalize_source_card({"id": "9", "slug": "demo", "title": "Demo", "coverCard": "/uploads/covers/demo_card.webp"})
    assert result["href"] == "https://manhwahq.com/title/demo"
    assert result["cover"]["url"].endswith("/uploads/covers/demo_card.webp")


def test_compact_counts():
    assert parse_compact_count("184.2K") == 184200
    assert parse_compact_count("1.4M") == 1400000
    assert parse_compact_count("12,345") == 12345
