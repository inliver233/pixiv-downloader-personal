#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_config(config_path: str):
    from common.PixivConfig import PixivConfig  # pylint: disable=import-error

    cfg = PixivConfig()
    cfg.loadConfig(config_path)
    return cfg


def _resolve_db_path(args) -> str:
    if args.db:
        return args.db

    cfg = _load_config(args.config)
    db_path = getattr(cfg, "dbPath", "") or ""
    return db_path


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _export_ids(db_path: str, out_path: Path) -> int:
    from PixivDBManager import PixivDBManager  # pylint: disable=import-error

    db = PixivDBManager(str(REPO_ROOT), target=db_path)
    try:
        # Make sure the follow table exists.
        db.createDatabase()
        member_ids = db.selectFollowMemberIds()
    finally:
        db.close()

    _ensure_parent_dir(out_path)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fp:
        for member_id in member_ids:
            fp.write(f"{member_id}\n")

    return len(member_ids)


def _export_ids_with_name(db_path: str, out_path: Path) -> int:
    import sqlite3

    resolved_db_path = db_path
    if not resolved_db_path:
        resolved_db_path = str(REPO_ROOT / "db.sqlite")
    resolved_db_path = os.path.abspath(resolved_db_path)

    conn = sqlite3.connect(resolved_db_path, timeout=5 * 60)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT member_id, COALESCE(name, ''), COALESCE(member_token, '') "
            "FROM pixiv_follow_member ORDER BY member_id"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    _ensure_parent_dir(out_path)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fp:
        for member_id, name, member_token in rows:
            fp.write(f"{int(member_id)}\t{name}\t{member_token}\n")

    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从本项目的 SQLite 数据库中导出关注列表 (pixiv_follow_member) 到 txt，一行一个用户 ID。",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config.ini"),
        help="config.ini 路径（用于读取 Settings.dbPath；可选）。默认: 仓库根目录/config.ini",
    )
    parser.add_argument(
        "--db",
        default="",
        help="直接指定 db.sqlite 路径（优先级高于 --config 里的 dbPath）。为空则用默认 db.sqlite。",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "following_member_ids.txt"),
        help="输出 txt 路径。默认: 关注/following_member_ids.txt",
    )
    parser.add_argument(
        "--with-name",
        action="store_true",
        help="输出为: member_id<TAB>name<TAB>member_token（仍是一行一个）。",
    )
    args = parser.parse_args(argv)

    db_path = _resolve_db_path(args)
    out_path = Path(args.out).resolve()

    if args.with_name:
        count = _export_ids_with_name(db_path, out_path)
    else:
        count = _export_ids(db_path, out_path)

    print(f"已导出 {count} 个关注到: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

