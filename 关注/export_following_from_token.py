#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_config(config_path: str):
    from common.PixivConfig import PixivConfig  # pylint: disable=import-error

    cfg = PixivConfig()
    cfg.loadConfig(config_path)
    return cfg


def _mask_secret(secret: str, keep: int = 4) -> str:
    if not secret:
        return ""
    if len(secret) <= keep * 2:
        return "*" * len(secret)
    return f"{secret[:keep]}...{secret[-keep:]}"


def _login_oauth(cfg, refresh_token: str):
    from common.PixivOAuth import PixivOAuth  # pylint: disable=import-error

    proxies = cfg.proxy if getattr(cfg, "useProxy", False) else None
    username = (getattr(cfg, "username", "") or "").strip() or "refresh_token_login"
    password = (getattr(cfg, "password", "") or "").strip() or "refresh_token_login"
    oauth = PixivOAuth(
        username,
        password,
        proxies=proxies,
        validate_ssl=getattr(cfg, "enableSSLVerification", True),
        refresh_token=refresh_token,
    )
    resp = oauth.login()
    status = int(getattr(resp, "status_code", -1))
    if status != 200:
        text = getattr(resp, "text", "") or ""
        raise RuntimeError(f"OAuth 登录失败: HTTP {status} {text[:300]}")
    return oauth, resp


def _get_user_id_from_login_response(login_text: str) -> int:
    """
    Pixiv OAuth response is expected to look like:
    {"response":{"access_token":"...","refresh_token":"...","user":{"id":"123", ...}}}
    """
    try:
        data = json.loads(login_text)
    except ValueError as exc:
        raise ValueError(f"OAuth 返回不是 JSON：{login_text[:200]}") from exc
    response = data.get("response") if isinstance(data, dict) else None
    if not isinstance(response, dict):
        raise ValueError("OAuth 返回数据缺少 response 字段")
    user = response.get("user")
    if not isinstance(user, dict):
        raise ValueError("OAuth 返回数据缺少 user 字段")
    user_id = user.get("id")
    if user_id is None:
        raise ValueError("OAuth 返回数据缺少 user.id")
    return int(user_id)


def _extract_following_user_ids(payload: dict) -> Iterable[int]:
    """
    /v1/user/following response usually contains:
      - user_previews: [{user: {id: ...}, illusts: [...]}]
    """
    previews = payload.get("user_previews")
    if isinstance(previews, list):
        for item in previews:
            if not isinstance(item, dict):
                continue
            user = item.get("user")
            if not isinstance(user, dict):
                continue
            uid = user.get("id")
            if uid is None:
                continue
            try:
                yield int(uid)
            except (TypeError, ValueError):
                continue

    # Fallback shapes (best-effort)
    users = payload.get("users")
    if isinstance(users, list):
        for user in users:
            if not isinstance(user, dict):
                continue
            uid = user.get("id")
            if uid is None:
                continue
            try:
                yield int(uid)
            except (TypeError, ValueError):
                continue


def _get_next_url(payload: dict) -> Optional[str]:
    next_url = payload.get("next_url")
    if isinstance(next_url, str) and next_url.strip():
        return next_url.strip()
    return None


def _fetch_following_ids(oauth, user_id: int, restrict: str, *, page_sleep: float) -> list[int]:
    from requests import RequestException  # pylint: disable=import-error

    url = "https://app-api.pixiv.net/v1/user/following"
    headers = oauth._get_headers_with_bearer()  # pylint: disable=protected-access
    proxies = oauth._proxies  # pylint: disable=protected-access
    verify = oauth._validate_ssl  # pylint: disable=protected-access

    all_ids: list[int] = []
    seen: set[int] = set()

    def do_get(target_url: str, params=None):
        nonlocal headers
        max_retries = 3
        base_wait = 5
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = oauth._req.get(  # pylint: disable=protected-access
                    target_url,
                    headers=headers,
                    params=params,
                    proxies=proxies,
                    verify=verify,
                    timeout=60,
                )
            except RequestException as exc:
                last_exc = exc
                time.sleep(max(1, base_wait) * attempt)
                continue

            status = int(getattr(resp, "status_code", -1))
            if status == 401:
                oauth.login()
                headers = oauth._get_headers_with_bearer()  # pylint: disable=protected-access
                time.sleep(1)
                continue
            if status in (429, 500, 502, 503, 504):
                time.sleep(max(1, base_wait) * attempt)
                continue
            return resp

        if last_exc is not None:
            raise RuntimeError(f"网络请求失败（重试{max_retries}次仍失败）: {target_url}") from last_exc
        return resp  # type: ignore[UnboundLocalVariable]

    resp = do_get(url, params={"user_id": str(int(user_id)), "restrict": restrict})
    page = 1
    while True:
        status = int(getattr(resp, "status_code", -1))
        text = getattr(resp, "text", "") or ""

        if status != 200:
            raise RuntimeError(f"获取关注列表失败: HTTP {status} {text[:300]}")

        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise RuntimeError(f"关注列表返回不是 JSON：{text[:200]}") from exc
        added = 0
        for uid in _extract_following_user_ids(payload):
            if uid in seen:
                continue
            seen.add(uid)
            all_ids.append(uid)
            added += 1

        print(f"[{restrict}] page={page} +{added} total={len(all_ids)}")

        next_url = _get_next_url(payload)
        if not next_url:
            break

        page += 1
        if page_sleep > 0:
            time.sleep(page_sleep)
        resp = do_get(next_url)

    return all_ids


def _write_ids(out_path: Path, ids: Iterable[int]) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(out_path, "w", encoding="utf-8", newline="\n") as fp:
        for uid in ids:
            fp.write(f"{int(uid)}\n")
            count += 1
    return count


def main(argv: Optional[list[str]] = None) -> int:
    default_out = Path(__file__).resolve().parent / "following_member_ids.txt"

    parser = argparse.ArgumentParser(
        description="使用 refresh_token 获取当前账号关注列表，并保存为 txt（一行一个用户ID）。",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config.ini"),
        help="config.ini 路径（用于代理/SSL 等设置；refresh_token 也可从这里读取）。默认: 仓库根目录/config.ini",
    )
    parser.add_argument(
        "--refresh-token",
        default="",
        help="Pixiv OAuth refresh_token（不传则读取 config.ini 的 [Authentication] refresh_token）。",
    )
    parser.add_argument(
        "--out",
        default=str(default_out),
        help=f"输出 txt 路径。默认: {default_out}",
    )
    parser.add_argument(
        "--restrict",
        choices=["all", "public", "private"],
        default="all",
        help="导出范围：public/private/all。默认: all",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="翻页时休眠秒数（避免风控）。默认: 1.0",
    )
    args = parser.parse_args(argv)

    cfg = _load_config(args.config)
    refresh_token = (args.refresh_token or getattr(cfg, "refresh_token", "") or "").strip()
    if not refresh_token:
        raise RuntimeError("refresh_token 为空：请用 --refresh-token 传入，或写入 config.ini 的 [Authentication] refresh_token。")

    print(f"使用 refresh_token={_mask_secret(refresh_token)} 登录中…")
    oauth, login_resp = _login_oauth(cfg, refresh_token)
    user_id = _get_user_id_from_login_response(getattr(login_resp, "text", "") or "")
    print(f"当前账号 user_id={user_id}")

    out_path = Path(args.out).resolve()

    restricts = ["public", "private"] if args.restrict == "all" else [args.restrict]
    ids: list[int] = []
    seen: set[int] = set()
    for restrict in restricts:
        for uid in _fetch_following_ids(oauth, user_id, restrict, page_sleep=float(args.sleep)):
            if uid in seen:
                continue
            seen.add(uid)
            ids.append(uid)

    count = _write_ids(out_path, ids)
    print(f"完成：共导出 {count} 个关注到 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
