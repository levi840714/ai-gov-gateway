import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from ai_gov_gateway.engine.approvals import (
    ApprovalDetail,
    ApprovalError,
    ApprovalStatusView,
    AuditVerification,
)
from ai_gov_gateway.engine.models import Approval, ApprovalStatus, UserIdentity
from ai_gov_gateway.engine.store import StoreError, StoreUnavailable
from ai_gov_gateway.engine.tokens import TokenError
from ai_gov_gateway.runtime import Services, get_services

router = APIRouter(prefix="/governance", tags=["governance"])
bearer = HTTPBearer(auto_error=False)


class ReviewBody(BaseModel):
    comment: str = Field(default="", max_length=2000)


async def resolve_services(request: Request) -> Services:
    # 經 dependency_overrides 查詢，測試才能替換 get_services
    provider = request.app.dependency_overrides.get(get_services, get_services)
    try:
        return await provider()
    except StoreUnavailable as error:
        raise HTTPException(503, "governance_store_unavailable") from error
    except StoreError as error:
        raise HTTPException(500, "governance_store_error") from error


ServicesDep = Annotated[Services, Depends(resolve_services)]


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> UserIdentity:
    # 審批以 IdP 的使用者身份驗證，不使用 LiteLLM virtual key：maker-checker 需要知道核准者是誰
    if credentials is None:
        raise HTTPException(status_code=401, detail="missing_token")
    services = await resolve_services(request)
    try:
        return await asyncio.to_thread(services.verifier.verify, credentials.credentials)
    except TokenError as error:
        raise HTTPException(status_code=401, detail="invalid_token") from error


UserDep = Annotated[UserIdentity, Depends(current_user)]


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, ApprovalError):
        return HTTPException(status_code=error.status_code, detail=error.code)
    if isinstance(error, StoreUnavailable):
        return HTTPException(status_code=503, detail="governance_store_unavailable")
    return HTTPException(status_code=500, detail="governance_store_error")


FAILURES = (ApprovalError, StoreUnavailable, StoreError)


@router.get("/approvals", response_model=list[Approval])
async def list_approvals(
    user: UserDep, services: ServicesDep, status: ApprovalStatus | None = None
) -> list[Approval]:
    try:
        return await services.approvals.list(user, status)
    except FAILURES as error:
        raise _http_error(error) from error


@router.get("/approvals/{approval_id}", response_model=ApprovalDetail)
async def get_approval(approval_id: str, user: UserDep, services: ServicesDep) -> ApprovalDetail:
    try:
        return await services.approvals.get(user, approval_id)
    except FAILURES as error:
        raise _http_error(error) from error


@router.get("/approvals/{approval_id}/status", response_model=ApprovalStatusView)
async def approval_status(
    approval_id: str, user: UserDep, services: ServicesDep
) -> ApprovalStatusView:
    try:
        return await services.approvals.status(user, approval_id)
    except FAILURES as error:
        raise _http_error(error) from error


@router.post("/approvals/{approval_id}/approve", response_model=Approval)
async def approve(
    approval_id: str, body: ReviewBody, user: UserDep, services: ServicesDep
) -> Approval:
    try:
        return await services.approvals.approve(user, approval_id, body.comment)
    except FAILURES as error:
        raise _http_error(error) from error


@router.post("/approvals/{approval_id}/reject", response_model=Approval)
async def reject(
    approval_id: str, body: ReviewBody, user: UserDep, services: ServicesDep
) -> Approval:
    try:
        return await services.approvals.reject(user, approval_id, body.comment)
    except FAILURES as error:
        raise _http_error(error) from error


@router.get("/audit/verify", response_model=AuditVerification)
async def verify_audit(user: UserDep, services: ServicesDep) -> AuditVerification:
    try:
        return await services.approvals.verify_audit(user)
    except FAILURES as error:
        raise _http_error(error) from error
