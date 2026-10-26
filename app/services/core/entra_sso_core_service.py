"""Turn a validated Microsoft sign-in into a LuxUPT user (fork feature: Entra sign-in).

Membership of the admin or read group is the grant. The user row is created at the first
sign-in, and its role is set from the groups at every sign-in, so a change in Entra applies the
next time the person signs in. Someone in neither group is refused, and any session they still
hold is ended.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.entra_client import EntraIdentity, EntraSignInError, role_for
from app.models.user_model import User

logger = logging.getLogger(__name__)

# A Microsoft-only account has no password. This verifies against nothing (it is not a hash), so
# the password form cannot sign in as it unless an admin deliberately sets a password on it.
NO_PASSWORD = "ENTRA_SSO"
MAX_USERNAME = 64


class EntraSsoCoreService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def sign_in(self, identity: EntraIdentity) -> User:
        """The LuxUPT user for this Microsoft identity, created or updated; raises
        EntraSignInError when the person has no access."""
        role = role_for(identity)
        user = await self._find(identity)

        if role is None:
            if user is not None and user.entra_oid == identity.object_id:
                # Removed from both groups: end any session they still hold.
                user.session_version += 1
                await self.db.flush()
            logger.warning(
                "entra_sign_in_refused",
                extra={"username": identity.username, "reason": "not in an access group"},
            )
            if identity.groups_overflowed:
                raise EntraSignInError(
                    "Your account is in too many groups for Microsoft to list them at sign-in. "
                    "Ask an administrator to grant access with an app role instead."
                )
            raise EntraSignInError(
                "Your Microsoft account has not been given access to LuxUPT. "
                "Ask a Timelapse administrator to add you."
            )

        if len(identity.username) > MAX_USERNAME:
            raise EntraSignInError(
                f"Your username is longer than {MAX_USERNAME} characters, which LuxUPT cannot store."
            )

        is_admin = role == "admin"
        if user is None:
            user = User(
                username=identity.username,
                password_hash=NO_PASSWORD,
                is_admin=is_admin,
                must_change_password=False,
                entra_oid=identity.object_id,
            )
            self.db.add(user)
            logger.info(
                "entra_user_created",
                extra={"username": identity.username, "role": role},
            )
        else:
            if user.entra_oid is None:
                # A local account with this exact username: link it to the Microsoft account.
                user.entra_oid = identity.object_id
                logger.info("entra_user_linked", extra={"username": user.username})
            if user.username != identity.username:
                # The UPN changed in Entra. A rename changes the session subject, so older
                # cookies end and the new one is issued below.
                if await self._username_taken(identity.username, user.id):
                    raise EntraSignInError(
                        f"Another LuxUPT account is already called {identity.username}."
                    )
                user.username = identity.username
            if user.is_admin != is_admin:
                logger.info(
                    "entra_role_changed",
                    extra={"username": user.username, "role": role},
                )
            user.is_admin = is_admin
            # Microsoft accounts never go through the local change-password form.
            user.must_change_password = False
        await self.db.flush()
        return user

    async def _find(self, identity: EntraIdentity) -> User | None:
        by_oid = (
            await self.db.execute(select(User).where(User.entra_oid == identity.object_id))
        ).scalar_one_or_none()
        if by_oid is not None:
            return by_oid
        by_name = (
            await self.db.execute(select(User).where(User.username == identity.username))
        ).scalar_one_or_none()
        if by_name is not None and by_name.entra_oid not in (None, identity.object_id):
            raise EntraSignInError(
                f"The LuxUPT account {identity.username} belongs to a different Microsoft account."
            )
        return by_name

    async def _username_taken(self, username: str, own_id: int) -> bool:
        other = (
            await self.db.execute(select(User.id).where(User.username == username))
        ).scalar_one_or_none()
        return other is not None and other != own_id
