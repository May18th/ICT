"""Nexent 平台管理 API 的最小客户端（在 `nexent-config` 容器内运行）。

为什么必须在容器里跑
-------------------
平台管理 API（`/api/...`）用 JWT（HS256，`SUPABASE_JWT_SECRET`）鉴权，签名密钥只存在于
`nexent-config` 容器环境；API Key 生成还依赖平台自身的 `database.token_db`。所以平台侧脚本
统一"拷进容器、用后端自己的 venv 跑"，不在宿主机伪造一套鉴权。

依赖约定
--------
`setup_nexent.py` / `upload_skills.py` 都以 `platform_client.py` 为唯一平台访问入口，
三者需一起拷进容器（同一目录），见 `deploy/nexent/README.md`。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_CONFIG_API = "http://localhost:5010"
DEFAULT_ADMIN_USER = "84b8db49-7c40-4287-8648-629ca1409693"


def config_api() -> str:
    """平台管理 API 基地址（容器网络内的 config 服务）。"""
    return os.getenv("NEXENT_CONFIG_API", DEFAULT_CONFIG_API)


def admin_user() -> str:
    """执行平台装配用的管理员用户 ID。"""
    return os.getenv("NEXENT_ADMIN_USER", DEFAULT_ADMIN_USER)


def mint_token(expires_in: int = 3600) -> str:
    """用容器内的 JWT 密钥签发一次管理 API 令牌。"""
    import jwt

    secret = os.getenv("SUPABASE_JWT_SECRET") or os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError("容器环境里没有 JWT_SECRET，无法调用平台管理 API")
    now = int(time.time())
    return jwt.encode(
        {"sub": admin_user(), "exp": now + expires_in, "iat": now, "role": "authenticated"},
        secret,
        algorithm="HS256",
    )


class PlatformClient:
    """带 JWT 的平台管理 API 客户端；只做 HTTP 转发，不含业务判断。"""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url or config_api()
        self.token = mint_token()

    def call(self, path: str, *, method: str = "GET", payload=None, timeout: int = 180):
        """JSON 请求。返回 `(status, body)`；网络异常返回 `(-1, "异常类型: 说明")`。"""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        return self._send(path, data=data, content_type="application/json", method=method, timeout=timeout)

    def call_multipart(
        self,
        path: str,
        *,
        fields: dict[str, str],
        file_field: str,
        filename: str,
        content: bytes,
        content_type: str = "text/markdown",
        method: str = "POST",
        timeout: int = 180,
    ):
        """`multipart/form-data` 请求（平台的文件上传端点只接受这种编码）。"""
        boundary = f"----nexent-upload-{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for key, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
            ).encode("utf-8")
        )
        chunks.append(content)
        chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        return self._send(
            path,
            data=b"".join(chunks),
            content_type=f"multipart/form-data; boundary={boundary}",
            method=method,
            timeout=timeout,
        )

    def _send(self, path: str, *, data: bytes | None, content_type: str, method: str, timeout: int):
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": content_type, "Authorization": f"Bearer {self.token}"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - 平台侧任何网络异常都要可读地回传
            return -1, f"{type(exc).__name__}: {exc}"
