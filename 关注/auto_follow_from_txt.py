#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class FollowResult:
    ok: bool
    status_code: int
    message: str
    raw_text: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_config(config_path: str):
    from common.PixivConfig import PixivConfig  # pylint: disable=import-error

    cfg = PixivConfig()
    cfg.loadConfig(config_path)
    return cfg


def _init_logging(cfg, *, debug: bool) -> None:
    import common.PixivHelper as PixivHelper  # pylint: disable=import-error

    cfg.logLevel = "DEBUG" if debug else "INFO"
    PixivHelper.set_config(cfg)
    PixivHelper.get_logger(reload=True)


def _mask_secret(secret: str, keep: int = 4) -> str:
    if not secret:
        return ""
    if len(secret) <= keep * 2:
        return "*" * len(secret)
    return f"{secret[:keep]}...{secret[-keep:]}"


def _iter_member_ids(txt_path: Path) -> Iterable[int]:
    # Accept formats:
    #  - "12345"
    #  - "12345\tname"
    #  - "id:12345 name:xxx"
    digit_re = re.compile(r"(\d+)")
    with open(txt_path, "r", encoding="utf-8") as fp:
        for lineno, line in enumerate(fp, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            m = digit_re.search(s)
            if not m:
                print(f"[跳过] {txt_path}:{lineno} 不是有效用户ID行: {s}")
                continue
            yield int(m.group(1))


def _read_id_set(path: Path) -> set[int]:
    if not path.exists():
        return set()
    ids: set[int] = set()
    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            s = line.strip()
            if not s:
                continue
            try:
                ids.add(int(s))
            except ValueError:
                continue
    return ids


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fp:
        fp.write(line)
        if not line.endswith("\n"):
            fp.write("\n")


def _append_jsonl(path: Path, payload: dict) -> None:
    _append_line(path, json.dumps(payload, ensure_ascii=False))


def _extract_error_message(text: str) -> str:
    # Pixiv API errors may look like:
    # {"error":{"user_message":"","message":"...","reason":"..."}}
    # {"errors":{"system":{"message":"..."}}}
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip()[:500]

    if isinstance(data, dict):
        err = data.get("error") or data.get("errors")
        if isinstance(err, dict):
            # try common shapes
            for key in ("user_message", "message", "reason"):
                val = err.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            system = err.get("system")
            if isinstance(system, dict):
                msg = system.get("message")
                if isinstance(msg, str) and msg.strip():
                    return msg.strip()
    return text.strip()[:500]


def _looks_like_already_followed(msg: str, raw_text: str) -> bool:
    hay = f"{msg}\n{raw_text}".lower()
    keywords = [
        "already",
        "already followed",
        "already added",
        "exists",
        "既に",
        "すでに",
        "フォロー",
        "已关注",
        "已经关注",
    ]
    return any(k.lower() in hay for k in keywords)


def _pixiv_follow_add(oauth, member_id: int, restrict: str):
    url = "https://app-api.pixiv.net/v1/user/follow/add"
    data = {"user_id": str(int(member_id)), "restrict": restrict}
    return oauth._req.post(  # pylint: disable=protected-access
        url,
        data=data,
        headers=oauth._get_headers_with_bearer(),  # pylint: disable=protected-access
        proxies=oauth._proxies,  # pylint: disable=protected-access
        verify=oauth._validate_ssl,  # pylint: disable=protected-access
        timeout=60,
    )


def _follow_one(
    oauth,
    member_id: int,
    restrict: str,
    *,
    max_retries: int,
    retry_wait: int,
) -> FollowResult:
    from requests import RequestException  # pylint: disable=import-error

    last_status = -1
    last_text = ""
    for attempt in range(1, max_retries + 1):
        try:
            resp = _pixiv_follow_add(oauth, member_id, restrict)
            last_status = int(getattr(resp, "status_code", -1))
            last_text = getattr(resp, "text", "") or ""

            if last_status == 200:
                return FollowResult(True, last_status, "OK", last_text)

            # Token might be expired; refresh and retry.
            if last_status == 401:
                oauth.login()
                continue

            msg = _extract_error_message(last_text)
            if last_status == 400 and _looks_like_already_followed(msg, last_text):
                return FollowResult(True, last_status, "ALREADY_FOLLOWED", last_text)

            # Retry on transient errors.
            if last_status in (429, 500, 502, 503, 504):
                time.sleep(max(1, retry_wait) * attempt)
                continue

            return FollowResult(False, last_status, msg, last_text)
        except RequestException as exc:
            last_status = -1
            last_text = str(exc)
            time.sleep(max(1, retry_wait) * attempt)
            continue

    msg = _extract_error_message(last_text)
    return FollowResult(False, last_status, f"RETRY_EXHAUSTED: {msg}", last_text)


def _login_oauth(cfg, refresh_token: str):
    from common.PixivOAuth import PixivOAuth  # pylint: disable=import-error

    proxies = cfg.proxy if getattr(cfg, "useProxy", False) else None
    username = (getattr(cfg, "username", "") or "").strip()
    password = (getattr(cfg, "password", "") or "").strip()
    # Upstream PixivOAuth requires non-empty username/password even when using refresh_token.
    # For our transfer tool we only need refresh_token flow, so fall back to placeholders.
    if not username:
        username = "refresh_token_login"
    if not password:
        password = "refresh_token_login"
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
    return oauth


def main(argv: Optional[list[str]] = None) -> int:
    default_input = Path(__file__).resolve().parent / "following_member_ids.txt"
    default_done = Path(__file__).resolve().parent / "follow_done.txt"
    default_failed = Path(__file__).resolve().parent / "follow_failed.txt"
    default_log = Path(__file__).resolve().parent / "auto_follow_log.jsonl"

    parser = argparse.ArgumentParser(
        description="使用 refresh_token 登录 Pixiv，并对 txt 内的用户逐个关注（可断点续跑）。",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config.ini"),
        help="config.ini 路径（用于代理/SSL 等设置；refresh_token 也可从这里读取）。默认: 仓库根目录/config.ini",
    )
    parser.add_argument(
        "--refresh-token",
        default="",
        help="Pixiv OAuth refresh_token（推荐命令行传入，避免写入 config.ini）。",
    )
    parser.add_argument(
        "--input",
        default=str(default_input),
        help=f"关注列表 txt，一行一个用户ID。默认: {default_input}",
    )
    parser.add_argument(
        "--restrict",
        choices=["public", "private"],
        default="public",
        help="关注的可见性。默认: public",
    )
    parser.add_argument(
        "--min-sleep",
        type=float,
        default=1.0,
        help="每次关注后最小休眠秒数（随机区间）。默认: 1.0",
    )
    parser.add_argument(
        "--max-sleep",
        type=float,
        default=2.5,
        help="每次关注后最大休眠秒数（随机区间）。默认: 2.5",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=3,
        help="单个用户的最大重试次数（遇到 429/5xx/网络错误时）。默认: 3",
    )
    parser.add_argument(
        "--retry-wait",
        type=int,
        default=5,
        help="重试等待秒数（会乘以重试次数作为退避）。默认: 5",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="本次最多关注多少个（0=不限制）。默认: 0",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="默认开启：跳过 follow_done.txt 中已成功的ID。",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="关闭断点续跑：不读取/写入 follow_done.txt。",
    )
    parser.add_argument(
        "--done",
        default=str(default_done),
        help=f"成功记录文件（每行一个ID）。默认: {default_done}",
    )
    parser.add_argument(
        "--failed",
        default=str(default_failed),
        help=f"失败记录文件（每行一个ID）。默认: {default_failed}",
    )
    parser.add_argument(
        "--log",
        default=str(default_log),
        help=f"JSONL 日志。默认: {default_log}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要关注的 ID，不发请求。",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="开启更详细日志（注意：DEBUG 可能会把 OAuth 响应写入 pixivutil.log）。",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="遇到任何关注失败立即停止。",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"找不到 input 文件: {input_path}")

    cfg = _load_config(args.config)
    _init_logging(cfg, debug=args.debug)

    refresh_token = (args.refresh_token or getattr(cfg, "refresh_token", "") or "").strip()
    if not refresh_token:
        raise RuntimeError("refresh_token 为空：请用 --refresh-token 传入，或写入 config.ini 的 [Authentication] refresh_token。")

    if args.debug:
        print(f"[WARN] debug 模式已开启，refresh_token 可能会被写入日志。当前 token: {_mask_secret(refresh_token)}")

    if args.dry_run:
        count = 0
        for member_id in _iter_member_ids(input_path):
            print(member_id)
            count += 1
            if args.limit and count >= args.limit:
                break
        print(f"dry-run 输出 {count} 行。")
        return 0

    oauth = _login_oauth(cfg, refresh_token)

    done_path = Path(args.done).resolve()
    failed_path = Path(args.failed).resolve()
    log_path = Path(args.log).resolve()

    resume_enabled = not args.no_resume
    done_ids = _read_id_set(done_path) if resume_enabled else set()

    processed = 0
    succeeded = 0
    skipped = 0
    failed = 0

    print(f"输入: {input_path}")
    print(f"restrict={args.restrict} retry={args.retry} sleep=[{args.min_sleep},{args.max_sleep}]s resume={resume_enabled}")
    print(f"done={done_path} failed={failed_path} log={log_path}")

    for member_id in _iter_member_ids(input_path):
        if resume_enabled and member_id in done_ids:
            skipped += 1
            continue

        if args.limit and succeeded >= args.limit:
            break

        processed += 1
        result = _follow_one(
            oauth,
            member_id,
            args.restrict,
            max_retries=max(1, int(args.retry)),
            retry_wait=max(1, int(args.retry_wait)),
        )

        log_payload = {
            "ts": _utc_now_iso(),
            "member_id": member_id,
            "ok": result.ok,
            "status_code": result.status_code,
            "message": result.message,
        }
        _append_jsonl(log_path, log_payload)

        if result.ok:
            succeeded += 1
            if resume_enabled:
                done_ids.add(member_id)
                _append_line(done_path, str(member_id))
            print(f"[OK] {member_id} {result.message}")
        else:
            failed += 1
            _append_line(failed_path, f"{member_id}\tHTTP {result.status_code}\t{result.message}")
            print(f"[FAIL] {member_id} HTTP {result.status_code} {result.message}")
            if args.stop_on_error:
                break

        # Sleep to avoid triggering rate limits.
        min_sleep = max(0.0, float(args.min_sleep))
        max_sleep = max(min_sleep, float(args.max_sleep))
        time.sleep(random.uniform(min_sleep, max_sleep))

    print(
        f"完成：processed={processed} succeeded={succeeded} failed={failed} skipped={skipped} "
        f"(input_total≈{processed + skipped})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
