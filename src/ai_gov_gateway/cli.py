import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_gov_gateway.cli_auth import (
    LoginError,
    load_token,
    poll_token,
    save_token,
    start_device_flow,
)
from ai_gov_gateway.engine.models import ApprovalStatus


class CliSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGW_", extra="ignore")

    gateway_url: str = "http://localhost:4000"
    oidc_issuer: str = "http://localhost:8080/realms/governance"
    client_id: str = "agw-cli"
    token_path: Path = Path.home() / ".config" / "ai-gov-gateway" / "token.json"


app = typer.Typer(no_args_is_help=True, help="AI Governance Gateway 的審批與稽核指令")
approvals_app = typer.Typer(no_args_is_help=True, help="審批")
audit_app = typer.Typer(no_args_is_help=True, help="稽核")
app.add_typer(approvals_app, name="approvals")
app.add_typer(audit_app, name="audit")


BAD_RESPONSE = "閘道回應格式不正確"


def make_client() -> httpx.Client:
    return httpx.Client(timeout=10)


def _fail(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(1)


def _unreachable(url: str, error: httpx.HTTPError) -> str:
    # 只印例外類別名稱：str(error) 可能含 URL 或標頭內容
    return f"無法連線到 {url}：{type(error).__name__}"


@app.command()
def login() -> None:
    """以 Keycloak device flow 登入。"""
    settings = CliSettings()
    with make_client() as http:
        try:
            code = start_device_flow(http, settings.oidc_issuer, settings.client_id)
            typer.echo(f"請在瀏覽器開啟：{code.verification_uri_complete or code.verification_uri}")
            typer.echo(f"驗證碼：{code.user_code}")
            token = poll_token(http, settings.oidc_issuer, settings.client_id, code)
        except LoginError as error:
            raise _fail(f"登入失敗：{error.code}") from error
        except httpx.HTTPError as error:
            raise _fail(_unreachable(settings.oidc_issuer, error)) from error
    save_token(settings.token_path, token)
    typer.echo("已登入")


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> Any:
    settings = CliSettings()
    try:
        token = load_token(settings.token_path)
    except LoginError as error:
        raise _fail("尚未登入，請先執行 agw login") from error
    try:
        with make_client() as http:
            response = http.request(
                method,
                f"{settings.gateway_url}/governance{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                params=params,
            )
    except httpx.HTTPError as error:
        raise _fail(_unreachable(settings.gateway_url, error)) from error
    if response.status_code == 401:
        raise _fail("token 無效或已過期，請重新執行 agw login")
    if response.status_code >= 400:
        raise _fail(f"失敗（{response.status_code}）：{_error_detail(response)}")
    try:
        return response.json()
    except ValueError as error:
        raise _fail(BAD_RESPONSE) from error


@contextmanager
def _expected_shape() -> Iterator[None]:
    """JSON 合法但欄位或型別不符時，以結束碼 1 結束而不是印出 traceback。"""
    try:
        yield
    except (KeyError, TypeError, AttributeError) as error:
        raise _fail(BAD_RESPONSE) from error


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    detail = body.get("detail", response.text) if isinstance(body, dict) else response.text
    # FastAPI 的 422 detail 是 list
    return detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)


def _line(row: dict[str, Any]) -> str:
    return (
        f"{row['id']}  {row['status']:<8}  {row['server']}.{row['tool']}  "
        f"委託人 {row['username']}  原因 {','.join(row['reasons'])}  到期 {row['expires_at']}"
    )


@approvals_app.command("list")
def list_approvals(
    status: Annotated[
        ApprovalStatus, typer.Option(help="pending、approved、rejected、expired、used")
    ] = ApprovalStatus.PENDING,
    show_all: Annotated[bool, typer.Option("--all", help="列出所有狀態")] = False,
) -> None:
    rows = _request("GET", "/approvals", params=None if show_all else {"status": status.value})
    if not isinstance(rows, list):
        raise _fail(BAD_RESPONSE)
    if not rows:
        typer.echo("沒有符合的審批")
    with _expected_shape():
        lines = [_line(row) for row in rows]
    for line in lines:
        typer.echo(line)


@approvals_app.command("show")
def show(approval_id: str) -> None:
    typer.echo(
        json.dumps(_request("GET", f"/approvals/{approval_id}"), ensure_ascii=False, indent=2)
    )


@approvals_app.command("approve")
def approve(approval_id: str, comment: Annotated[str, typer.Option()] = "") -> None:
    row = _request("POST", f"/approvals/{approval_id}/approve", body={"comment": comment})
    with _expected_shape():
        typer.echo(_line(row))


@approvals_app.command("reject")
def reject(approval_id: str, comment: Annotated[str, typer.Option()] = "") -> None:
    row = _request("POST", f"/approvals/{approval_id}/reject", body={"comment": comment})
    with _expected_shape():
        typer.echo(_line(row))


@audit_app.command("verify")
def verify() -> None:
    result = _request("GET", "/audit/verify")
    with _expected_shape():
        first_invalid_id, records = result["first_invalid_id"], result["records"]
    if first_invalid_id is not None:
        raise _fail(f"稽核紀錄被竄改：第一筆不一致的 id 為 {first_invalid_id}")
    typer.echo(f"稽核紀錄完整（{records} 筆）")
