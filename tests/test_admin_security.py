from app.admin.dependencies import ROLE_PERMISSIONS
from app.admin.router import router


def test_control_plane_routes_are_excluded_from_api_schema():
    assert router.include_in_schema is False
    assert all(not getattr(route, "include_in_schema", True) for route in router.routes)


def test_normal_users_have_no_control_plane_permissions():
    assert "user" not in ROLE_PERMISSIONS
    assert all("*" not in permissions for role, permissions in ROLE_PERMISSIONS.items() if role != "owner")


def test_only_owner_has_wildcard_permissions():
    assert ROLE_PERMISSIONS["owner"] == {"*"}
    assert "users.manage" in ROLE_PERMISSIONS["admin"]
    assert "users.manage" not in ROLE_PERMISSIONS["moderator"]
