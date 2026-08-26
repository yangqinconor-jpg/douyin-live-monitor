#!/usr/bin/env python3
"""Receive one Feishu OAuth callback and store the delegated user token."""

import argparse
import json
import logging
import os
import pwd
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def post(url: str, payload: dict, headers: dict | None = None) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        return json.load(urlopen(request, timeout=20))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Feishu API returned HTTP {error.code}: {detail}") from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--token-owner", default="douyin-live")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    output = Path(args.output)

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def reply(self, status: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            request = urlparse(self.path)
            if request.path == "/healthz":
                self.reply(200, "ok")
                return
            if request.path != "/feishu/oauth/callback":
                self.reply(404, "not found")
                return

            query = parse_qs(request.query)
            if query.get("state", [""])[0] != args.state:
                self.reply(400, "授权校验失败，请关闭此页面后重新发起授权。")
                return
            if "error" in query or not query.get("code"):
                self.reply(400, "飞书授权未完成，请关闭此页面后重新发起授权。")
                return

            try:
                app_tokens = post(
                    "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
                    {"app_id": config["feishu_app_id"], "app_secret": config["feishu_app_secret"]},
                )
                app_access_token = app_tokens.get("app_access_token")
                if app_tokens.get("code", 0) != 0 or not app_access_token:
                    raise RuntimeError(app_tokens.get("msg", "飞书未返回应用访问令牌"))
                user_tokens = post(
                    "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token",
                    {
                        "grant_type": "authorization_code",
                        "code": query["code"][0],
                        "app_access_token": app_access_token,
                    },
                )
                token_data = user_tokens.get("data", user_tokens)
                if user_tokens.get("code", 0) != 0 or not token_data.get("access_token"):
                    raise RuntimeError(user_tokens.get("msg", "飞书未返回用户授权令牌"))
                if not token_data.get("refresh_token"):
                    raise RuntimeError("飞书未返回刷新令牌，请为应用添加 offline_access 后重新授权")
                token_data["expires_at"] = time.time() + int(token_data.get("expires_in", 7200))
                temporary = output.with_suffix(".tmp")
                temporary.write_text(json.dumps(token_data, ensure_ascii=False))
                temporary.chmod(0o600)
                # The callback runs as root, while the monitor runs as the
                # restricted service account and must refresh this token.
                if os.geteuid() == 0:
                    owner = pwd.getpwnam(args.token_owner)
                    os.chown(temporary, owner.pw_uid, owner.pw_gid)
                temporary.replace(output)
            except Exception as error:
                logging.exception("Failed to exchange Feishu OAuth authorization code: %s", error)
                self.reply(500, "授权结果保存失败，请联系管理员查看服务器日志。")
                return
            self.reply(200, "<h2>授权成功</h2><p>可以关闭此页面，系统将继续验证妙记转写。</p>")

    server = ThreadingHTTPServer(("0.0.0.0", args.port), CallbackHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
