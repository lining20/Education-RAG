"""上传账号登录接口；账号通过本机管理命令创建，不开放公开注册。"""
import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from utils.clients.upload_auth import COOKIE_NAME, SESSION_SECONDS
from utils.path_util import PROJECT_ROOT

router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(PROJECT_ROOT / "page" / "login.html")


@router.post("/api/auth/login")
async def login(payload: LoginRequest, request: Request):
    token, username = await asyncio.to_thread(request.app.state.upload_auth.login,
        payload.username.strip(), payload.password, request.client.host if request.client else "unknown")
    response = JSONResponse({"username": username, "redirect": "/import"})
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/auth/me")
async def me(request: Request):
    user = await asyncio.to_thread(request.app.state.upload_auth.current_user, request.cookies.get(COOKIE_NAME))
    return JSONResponse({"authenticated": bool(user), "username": user["username"] if user else ""},
                        headers={"Cache-Control": "no-store"})


@router.post("/api/auth/logout")
async def logout(request: Request):
    await asyncio.to_thread(request.app.state.upload_auth.logout, request.cookies.get(COOKIE_NAME))
    response = JSONResponse({"redirect": "/"})
    response.delete_cookie(COOKIE_NAME)
    return response
