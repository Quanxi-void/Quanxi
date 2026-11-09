"""One-time migration for the one-row-per-user-and-manga reading history model.

Run once before starting multiple API workers:
    python -m scripts.migrate_reading_history
"""
import asyncio
import os

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING


async def migrate():
    load_dotenv()
    uri = os.getenv("MONGODB_URI", "").strip()
    database_name = os.getenv("MONGODB_DATABASE", "manhwahq").strip()
    if not uri:
        raise SystemExit("MONGODB_URI is required. Load your production .env first.")

    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000, appname="ManhwaHQ-History-Migration")
    try:
        await client.admin.command("ping")
        db = client[database_name]
        collection = db.reading_history
        indexes = await collection.index_information()

        legacy_name = "user_id_1_manga_slug_1_chapter_number_1"
        if legacy_name in indexes:
            await collection.drop_index(legacy_name)

        target_key = [("user_id", 1), ("manga_slug", 1)]
        for index_name, index_info in indexes.items():
            if index_info.get("key") == target_key and not index_info.get("unique", False):
                await collection.drop_index(index_name)

        # Keep the most recently read document for each user+manga pair.
        cursor = collection.find(
            {},
            {"_id": 1, "user_id": 1, "manga_slug": 1},
        ).sort([("read_at", DESCENDING), ("_id", DESCENDING)])

        seen = set()
        delete_batch = []
        deleted = 0
        async for doc in cursor:
            key = (doc.get("user_id"), doc.get("manga_slug"))
            if key in seen:
                delete_batch.append(doc["_id"])
            else:
                seen.add(key)
            if len(delete_batch) >= 1000:
                result = await collection.delete_many({"_id": {"$in": delete_batch}})
                deleted += result.deleted_count
                delete_batch.clear()

        if delete_batch:
            result = await collection.delete_many({"_id": {"$in": delete_batch}})
            deleted += result.deleted_count

        await collection.create_index(
            [("user_id", ASCENDING), ("manga_slug", ASCENDING)],
            unique=True,
        )
        await collection.create_index(
            [("user_id", ASCENDING), ("read_at", DESCENDING)],
        )
        print(f"Migration complete. Removed {deleted} duplicate history records.")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(migrate())
