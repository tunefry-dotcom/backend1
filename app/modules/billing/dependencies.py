"""Feature-gate dependency for domain routes.

``require_feature(Feature.X)`` returns a FastAPI dependency that 403s unless the
current user's plan grants feature X. Domain endpoints (releases, transfers,
pitching — built later) attach it so entitlement enforcement lives server-side,
not only in the frontend router.

Usage::

    @router.post("/releases/album",
                 dependencies=[Depends(require_feature(Feature.RELEASE_ALBUM))])
    async def create_album(...): ...
"""

from __future__ import annotations

from typing import Annotated, Callable

from fastapi import Depends, HTTPException, status

from app.modules.auth.dependencies import CurrentUser, get_current_user
from app.modules.billing.plans import Feature, has_feature, min_plan_for


def require_feature(feature: Feature) -> Callable[..., CurrentUser]:
    """Build a dependency that enforces ``feature`` for the current plan."""

    async def _guard(
        current_user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> CurrentUser:
        if not has_feature(current_user.plan, feature):
            unlock = min_plan_for(feature)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "plan_upgrade_required",
                    "feature": feature.value,
                    "current_plan": current_user.plan.value,
                    "required_plan": unlock.value if unlock else None,
                },
            )
        return current_user

    return _guard
