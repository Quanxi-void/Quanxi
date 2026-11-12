from datetime import datetime, timezone

from app.models.common import APIResponse, ResponseMeta
from app.users.schemas import TokenPair, UserProfile


def test_refresh_response_accepts_token_pair_model():
    user = UserProfile(
        id="user-123",
        username="reader",
        display_name="Reader",
        avatar={"kind": "preset", "value": "Avatar-20(1).jpg", "url": "/avatar.jpg"},
        joined_at=datetime.now(timezone.utc),
        is_public=True,
        email="reader@example.com",
    )
    pair = TokenPair(
        access_token="access-token",
        refresh_token="refresh-token",
        expires_in=1200,
        user=user,
    )

    response = APIResponse[TokenPair](data=pair, meta=ResponseMeta(request_id="req_test"))
    payload = response.model_dump(mode="json")

    assert payload["success"] is True
    assert payload["data"]["access_token"] == "access-token"
    assert payload["data"]["refresh_token"] == "refresh-token"
    assert payload["data"]["user"]["username"] == "reader"
