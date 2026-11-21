"""Bearer-token scheme that keeps Céluma's 403 contract for missing credentials.

FastAPI's `HTTPBearer(auto_error=True)` answered a missing, empty or
non-Bearer `Authorization` header with **403** up to 0.116.x. Newer releases
answer **401** with a `WWW-Authenticate` header instead.

Céluma's API contract is the 403. The frontend's `apiFetch` treats every 401 as
an expired session and redirects to the login page, and the HTTP tests lock
the 403 (`tests/http/test_block_d_session_expiry.py`,
`tests/http/test_block_d_portal_isolation.py`,
`tests/http/test_assignable_users.py`). A dependency upgrade must not change
that, so this subclass reproduces the 0.116.2 behaviour exactly: same status
codes, same `detail` strings, no `WWW-Authenticate` header.

Changing the contract to 401 is a separate decision for the API owner, not a
side effect of upgrading FastAPI.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security.utils import get_authorization_scheme_param
from starlette.requests import HTTPConnection
from starlette.status import HTTP_403_FORBIDDEN


class ForbiddenHTTPBearer(HTTPBearer):
    """`HTTPBearer` that rejects missing or malformed credentials with 403."""

    async def __call__(
        self, request: HTTPConnection
    ) -> Optional[HTTPAuthorizationCredentials]:
        authorization = request.headers.get("Authorization")
        scheme, credentials = get_authorization_scheme_param(authorization)
        if not (authorization and scheme and credentials):
            if self.auto_error:
                raise HTTPException(
                    status_code=HTTP_403_FORBIDDEN, detail="Not authenticated"
                )
            return None
        if scheme.lower() != "bearer":
            if self.auto_error:
                raise HTTPException(
                    status_code=HTTP_403_FORBIDDEN,
                    detail="Invalid authentication credentials",
                )
            return None
        return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)
