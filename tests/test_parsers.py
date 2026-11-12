from app.scraper.parsers import parse_manga_detail, parse_reader, parse_home, parse_browse

DETAIL_HTML = """
<html><head>
<meta property="og:title" content="Example Series">
<meta property="og:image" content="/uploads/covers/example.webp">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ComicSeries","name":"Example Series",
"description":"A synthetic test description","alternateName":["Example Alt"],
"author":{"@type":"Person","name":"Writer"},"datePublished":"2022"}
</script></head><body><main>
<h1>Example Series</h1>
<div><span>Status</span><span>Ongoing</span></div>
<div><span>Type</span><span>Manhwa</span></div>
<a href="/title/example-series/chapter/12">Ch. 12</a>
<a href="/title/example-series/chapter/11">Ch. 11</a>
</main></body></html>
"""

READER_HTML = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"ComicIssue","issueNumber":"1",
"name":"Example Series Chapter 1","datePublished":"2024-01-01",
"isPartOf":{"@type":"ComicSeries","name":"Example Series"}}
</script></head><body>
<h1>Example Series Chapter 1</h1>
<img id="page-0" alt="Page 1" src="/reader/p001.jpg" loading="eager">
<img id="page-1" alt="Page 2" src="/reader/p002.jpg" loading="lazy">
</body></html>
"""


def test_parse_manga_detail():
    data = parse_manga_detail(DETAIL_HTML, "https://manhwahq.com/title/example-series")
    assert data["slug"] == "example-series"
    assert data["title"]["primary"] == "Example Series"
    assert data["description"] == "A synthetic test description"
    assert data["images"]["cover"]["url"] == "https://manhwahq.com/uploads/covers/example.webp"
    assert len(data["chapters"]) == 2
    assert data["chapters"][0]["chapter_number"] == 12


def test_parse_reader_order_and_absolute_urls():
    data = parse_reader(READER_HTML, "https://manhwahq.com/title/example-series/chapter/1", "example-series", "1")
    assert data["page_count"] == 2
    assert data["pages"][0]["page_number"] == 1
    assert data["pages"][0]["image"]["url"] == "https://manhwahq.com/reader/p001.jpg"
    assert data["pages"][1]["index"] == 1


def test_parse_home_does_not_fail_on_empty_page():
    data = parse_home("<html><body><main><h1>Home</h1></main></body></html>", "https://manhwahq.com/")
    assert data["hero"] == []
    assert isinstance(data["sections"], list)


def test_parse_browse_empty_page():
    data = parse_browse("<html><body><main><h1>Browse</h1></main></body></html>", "https://manhwahq.com/browse")
    assert data["items"] == []


def test_home_card_extracts_latest_chapter_update_time_and_type_without_rating_status():
    from app.models.manga import HomeData

    html = '''
    <html><body><main>
      <h2>Latest Updates</h2>
      <div>
        <a class="group flex flex-col" href="/title/overgeared-2020-manhwa">
          <div class="relative aspect-[193/252]">
            <img src="https://b.toonking.xyz/uploads/covers/overgeared_card.webp"
                 alt="Overgeared" width="250" height="326" />
            <div class="absolute top-1.5 left-1.5">
              <span class="font-medium tracking-wide leading-none"
                    style="color: rgb(255, 255, 255);">Manhwa</span>
            </div>
          </div>
          <div class="flex flex-col gap-1 px-1">
            <div class="flex justify-between items-center text-zinc-500">
              <span>Ch. 347</span>
              <span>7min ago</span>
            </div>
            <h3>Overgeared</h3>
          </div>
        </a>
      </div>
    </main></body></html>
    '''

    second_card = '''
      <a class="group flex flex-col" href="/title/another-manhwa">
        <div><img src="https://b.toonking.xyz/uploads/covers/another_card.webp" alt="Another Manhwa" /></div>
        <div class="flex justify-between items-center"><span>Ch. 12</span><span>8min ago</span></div>
        <span class="font-medium tracking-wide leading-none">Manga</span>
        <h3>Another Manhwa</h3>
      </a>
    '''
    html = html.replace("</main>", second_card + "</main>")
    parsed = parse_home(html, "https://manhwahq.com/")
    card = next(item for item in parsed["latest_updates"] if item["slug"] == "overgeared-2020-manhwa")
    assert card["latest_chapter"] == "Ch. 347"
    assert card["updated_at"] == "7min ago"
    assert card["type"] == "Manhwa"
    assert "rating" not in card
    assert "status" not in card

    # Verify the actual API response model also omits these fields.
    serialized = HomeData.model_validate(parsed).model_dump()
    serialized_card = serialized["latest_updates"][0]
    assert serialized_card["type"] == "Manhwa"
    assert "rating" not in serialized_card
    assert "status" not in serialized_card



def test_parse_manga_detail_extracts_related_section_only():
    from app.models.manga import MangaDetails

    html = '''
    <html><body><main>
      <h1>Current Series</h1>
      <!-- This unrelated card must not leak into recommendations. -->
      <div>
        <a href="/title/unrelated-card">
          <img src="https://b.toonking.xyz/uploads/covers/unrelated.webp" alt="Unrelated" />
          <h3>Unrelated Card</h3>
        </a>
      </div>
      <section class="mt-8 mb-2">
        <div class="flex items-center justify-between mb-4">
          <h2>Related</h2>
          <a href="/browse?genre=Action">More</a>
        </div>
        <div class="grid grid-cols-2">
          <div class="w-full">
            <a class="group flex flex-col" href="/title/the-game-that-i-came-from">
              <div class="relative aspect-[193/252]">
                <img src="https://b.toonking.xyz/uploads/covers/the-game-that-i-came-from_card.webp?v=1"
                     alt="The Game That I Came From" width="250" height="326" />
                <div class="absolute top-1.5 left-1.5">
                  <span class="font-medium tracking-wide leading-none">Manhua</span>
                </div>
              </div>
              <div class="flex flex-col gap-1">
                <div class="flex justify-between items-center text-zinc-500">
                  <span>Ch. 359</span><span>1y ago</span>
                </div>
                <h3>The Game That I Came From</h3>
              </div>
            </a>
          </div>
          <div class="w-full">
            <a class="group flex flex-col" href="/title/secretly-cultivate-for-a-thousand-years">
              <div class="relative aspect-[193/252]">
                <img src="https://b.toonking.xyz/uploads/covers/secretly-cultivate_card.webp?v=1"
                     alt="Top Tier Providence" />
                <div class="absolute top-1.5 left-1.5">
                  <span class="font-medium tracking-wide leading-none">Manhua</span>
                </div>
              </div>
              <div class="flex flex-col gap-1">
                <div class="flex justify-between items-center text-zinc-500">
                  <span>Ch. 284</span><span>5d ago</span>
                </div>
                <h3>Top Tier Providence - Secretly Cultivate for a Thousand Years</h3>
              </div>
            </a>
          </div>
        </div>
      </section>
    </main></body></html>
    '''

    parsed = parse_manga_detail(
        html,
        "https://manhwahq.com/title/current-series",
    )
    recommendations = parsed["recommendations"]

    assert len(recommendations) == 2
    first = recommendations[0]
    assert first["slug"] == "the-game-that-i-came-from"
    assert first["title"] == "The Game That I Came From"
    assert first["cover"]["url"] == (
        "https://b.toonking.xyz/uploads/covers/the-game-that-i-came-from_card.webp?v=1"
    )
    assert first["type"] == "Manhua"
    assert first["latest_chapter"] == "Ch. 359"
    assert first["updated_at"] == "1y ago"
    assert first["href"] == "https://manhwahq.com/title/the-game-that-i-came-from"
    assert all(item["slug"] != "unrelated-card" for item in recommendations)
    assert all("rating" not in item and "status" not in item for item in recommendations)

    # Ensure the typed detail response keeps the recommendation-specific fields.
    model = MangaDetails.model_validate(parsed)
    serialized = model.model_dump()
    assert serialized["recommendations"][0]["type"] == "Manhua"
    assert serialized["recommendations"][0]["latest_chapter"] == "Ch. 359"
