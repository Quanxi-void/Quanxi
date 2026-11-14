from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings

_client: AsyncIOMotorClient | None = None
_database = None


async def connect_database() -> None:
    """Connect and ensure indexes only; data migrations run separately once."""
    global _client, _database
    if not settings.mongodb_uri:
        return
    if len(settings.jwt_secret) < 32 or settings.jwt_secret == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET_IN_PRODUCTION":
        raise RuntimeError("Set a strong JWT_SECRET of at least 32 characters before enabling user accounts.")

    from pymongo import ASCENDING, DESCENDING, IndexModel

    _client = AsyncIOMotorClient(
        settings.mongodb_uri,
        serverSelectionTimeoutMS=settings.mongodb_connect_timeout_ms,
        connectTimeoutMS=settings.mongodb_connect_timeout_ms,
        socketTimeoutMS=settings.mongodb_socket_timeout_ms,
        maxPoolSize=settings.mongodb_max_pool_size,
        minPoolSize=settings.mongodb_min_pool_size,
        waitQueueTimeoutMS=settings.mongodb_wait_queue_timeout_ms,
        retryWrites=True,
        appname="ManhwaHQ-API",
    )
    _database = _client[settings.mongodb_database]
    await _client.admin.command("ping")

    await _database.users.create_indexes([
        IndexModel([("email", ASCENDING)], unique=True),
        IndexModel([("username", ASCENDING)], unique=True),
    ])
    await _database.bookmarks.create_index(
        [("user_id", ASCENDING), ("manga_slug", ASCENDING)],
        unique=True,
    )
    # The one-time migration script must remove legacy duplicates and create this
    # unique index before the upgraded API is deployed to an existing database.
    await _database.reading_history.create_index(
        [("user_id", ASCENDING), ("manga_slug", ASCENDING)],
        unique=True,
    )
    await _database.reading_history.create_index(
        [("user_id", ASCENDING), ("read_at", DESCENDING)],
    )
    await _database.bookmarks.create_index(
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
    )
    await _database.activity_logs.create_index(
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
    )
    activity_indexes = await _database.activity_logs.index_information()
    for index_name, index_info in activity_indexes.items():
        if index_info.get("key") == [("created_at", 1)]:
            if index_info.get("expireAfterSeconds") != settings.activity_log_ttl_seconds:
                await _database.activity_logs.drop_index(index_name)
    await _database.activity_logs.create_index(
        "created_at",
        expireAfterSeconds=settings.activity_log_ttl_seconds,
    )
    await _database.sessions.create_index(
        [("jti", ASCENDING)], unique=True
    )
    await _database.sessions.create_index(
        "expires_at", expireAfterSeconds=0
    )

    # Private administration data and editor overrides. These collections are
    # created by indexes only; no existing user/content documents are rewritten.
    await _database.admin_audit_logs.create_index([("created_at", DESCENDING)])
    await _database.admin_audit_logs.create_index([("actor_id", ASCENDING), ("created_at", DESCENDING)])
    await _database.manga_overrides.create_index([("slug", ASCENDING)], unique=True)
    await _database.chapter_overrides.create_index([("manga_slug", ASCENDING), ("chapter_number", ASCENDING)], unique=True)


async def close_database() -> None:
    global _client, _database
    if _client:
        _client.close()
    _client = None
    _database = None


def get_database():
    if _database is None:
        raise RuntimeError("MongoDB is not connected. Configure MONGODB_URI in .env.")
    return _database
