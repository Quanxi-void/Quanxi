from fastapi import Depends, HTTPException
from app.auth.dependencies import current_user

ROLE_PERMISSIONS = {
    "owner": {"*"},
    "admin": {
        "dashboard.read", "content.read", "content.write", "users.read", "users.manage",
        "homepage.read", "homepage.write", "system.read", "scraper.run", "cache.manage",
        "settings.read", "settings.write", "audit.read",
    },
    "content_manager": {"dashboard.read", "content.read", "content.write", "homepage.read", "homepage.write"},
    "moderator": {"dashboard.read", "users.read", "users.moderate", "audit.read"},
}

async def admin_user(user=Depends(current_user)):
    role = user.get("role", "user")
    if role not in ROLE_PERMISSIONS:
        raise HTTPException(status_code=404, detail="Not found.")
    return user

def require_permission(permission: str):
    async def dependency(user=Depends(admin_user)):
        permissions = ROLE_PERMISSIONS.get(user.get("role", "user"), set())
        if "*" not in permissions and permission not in permissions:
            raise HTTPException(status_code=403, detail="You do not have permission to perform this action.")
        return user
    return dependency

async def owner_user(user=Depends(admin_user)):
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Owner access required.")
    return user
