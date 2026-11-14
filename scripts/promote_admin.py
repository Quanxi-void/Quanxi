"""Promote an existing ManhwaVoid account to an administrative role.

Run from the API project root after configuring the production .env:
    python -m scripts.promote_admin --email you@example.com --role owner
"""
import argparse
import asyncio

from app.services.database import connect_database, close_database, get_database
from app.users.repository import utcnow

ROLES = {"owner", "admin", "content_manager", "moderator"}

async def promote(email: str, role: str, confirmed: bool) -> None:
    if not confirmed:
        answer = input(f"Promote existing account {email!r} to {role!r}? Type PROMOTE to continue: ")
        if answer != "PROMOTE":
            print("Cancelled; no account was changed.")
            return
    await connect_database()
    try:
        db = get_database()
        user = await db.users.find_one({"email": email.strip().lower()})
        if not user:
            raise SystemExit("No existing account matches that email. Register and verify the account first.")
        if not user.get("is_active", True):
            raise SystemExit("The account is inactive. Reactivate it before assigning an admin role.")
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"role": role, "updated_at": utcnow()}},
        )
        await db.admin_audit_logs.insert_one({
            "actor_id": "bootstrap-cli", "actor_username": "bootstrap-cli", "actor_role": "system",
            "action": "admin.role_bootstrapped", "target_type": "user", "target_id": str(user["_id"]),
            "details": {"email": email.strip().lower(), "role": role}, "created_at": utcnow(),
        })
        print(f"Updated {user.get('username')} to role {role!r}.")
    finally:
        await close_database()


def main() -> None:
    parser = argparse.ArgumentParser(description="Assign an admin role to an existing ManhwaVoid user.")
    parser.add_argument("--email", required=True, help="Email address of an already registered account")
    parser.add_argument("--role", choices=sorted(ROLES), default="owner")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation (for controlled deployments)")
    args = parser.parse_args()
    asyncio.run(promote(args.email, args.role, args.yes))

if __name__ == "__main__":
    main()
