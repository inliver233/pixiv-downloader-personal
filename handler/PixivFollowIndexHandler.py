# -*- coding: utf-8 -*-
import json
import os
import random
import shutil
import sys
import time
from typing import List, Optional, Sequence, Set, Tuple

import common.PixivHelper as PixivHelper
import handler.PixivDownloadHandler as PixivDownloadHandler
from common.PixivBrowserFactory import PixivBrowser
from common.PixivException import PixivException
from model.PixivArtist import PixivArtist


def _get_lang_param(locale: str) -> str:
    if not locale:
        return "en"
    return locale[1:] if locale.startswith("/") else locale


def _fetch_following_user_ids(br: PixivBrowser, rest: str) -> List[int]:
    member_id = br._myId
    if not member_id:
        raise PixivException("Missing my user id (not logged in?)", errorCode=PixivException.NOT_LOGGED_IN)

    ids: List[int] = []
    offset = 0
    limit = 48
    lang = _get_lang_param(br._locale)
    retry = getattr(getattr(br, "_config", None), "retry", 3)
    retry_wait = getattr(getattr(br, "_config", None), "retryWait", 5)

    while True:
        url = f"https://www.pixiv.net/ajax/user/{member_id}/following?offset={offset}&limit={limit}&rest={rest}&lang={lang}"
        PixivHelper.print_and_log("info", f"Fetching following list: {url}")

        payload = None
        last_error = None
        for attempt in range(0, int(retry) + 1):
            try:
                page_str = br.getPixivPage(url, enable_cache=False)
                payload = json.loads(page_str)
                last_error = None
                break
            except json.JSONDecodeError as ex:
                last_error = ex
                PixivHelper.print_and_log("warn", f"Invalid JSON for following list, retrying ({attempt + 1}/{retry + 1})...")
                PixivHelper.print_delay(min(2, int(retry_wait)))
            except PixivException as ex:
                last_error = ex
                PixivHelper.print_and_log("warn", f"Failed to fetch following list, retrying ({attempt + 1}/{retry + 1})... {ex}")
                PixivHelper.print_delay(min(2, int(retry_wait)))

        if payload is None:
            raise PixivException(f"Failed to fetch following list: {url}, last_error={last_error}", errorCode=PixivException.SERVER_ERROR)

        users = (payload.get("body") or {}).get("users") or []
        if not users:
            break

        for user in users:
            if user.get("isAdContainer"):
                continue
            try:
                ids.append(int(user["userId"]))
            except Exception:
                continue

        if len(users) < limit:
            break
        offset += limit

    return ids


def _get_member_profile_all(br: PixivBrowser, member_id: int) -> Tuple[PixivArtist, List[int]]:
    url = f"https://www.pixiv.net/ajax/user/{member_id}/profile/all"
    PixivHelper.print_and_log("info", f"Fetching member profile: {url}")
    response = br.getPixivPage(url, enable_cache=False)

    artist = PixivArtist(mid=int(member_id), page=response, fromImage=False, offset=0, limit=10**9)
    # PixivBrowser.getMemberInfoWhitecube() prefers using a "reference work id" to resolve account/token
    # via the web-rpc endpoint. When we only have profile/all, populate it from the first work if present
    # to avoid falling back to OAuth (which requires username/password and is often unavailable).
    if getattr(artist, "imageList", None):
        try:
            artist.reference_image_id = int(artist.imageList[0])
        except Exception:
            artist.reference_image_id = 0
    br.getMemberInfoWhitecube(member_id, artist, bookmark=False)
    return artist, list(artist.imageList)


def _build_preview_dir(root_directory: str) -> str:
    return os.path.abspath(os.path.join(root_directory, "_preview"))


def _download_preview_image(caller, member_id: int, image_id: int, page_index: int, url: str) -> Optional[str]:
    config = caller.__config__
    preview_root = _build_preview_dir(config.rootDirectory)
    ext = PixivHelper.get_extension_from_url(url)
    if not ext:
        ext = ".jpg"

    member_dir = os.path.join(preview_root, str(member_id))
    filename = os.path.join(member_dir, f"{image_id}_p{page_index}{ext}")

    PixivHelper.makeSubdirs(filename)
    result, _ = PixivDownloadHandler.download_image(
        caller=caller,
        url=url,
        filename=filename,
        referer="https://www.pixiv.net/",
        overwrite=False,
        max_retry=config.retry,
        backup_old_file=False,
        image=None,
        page=None,
        notifier=None,
    )
    if result is None:
        return None
    return filename


def sync_followed_artists(
    caller,
    config,
    bookmark_flag: str = "n",
    preview_per_artist: int = 3,
) -> None:
    """
    Sync followed artists into local DB:
    - Store ALL image urls into DB (no full downloads).
    - For NEW followed artist, download a few preview images (default: 3).
    - If an artist is no longer followed, delete their URL index + previews.
    """
    br: PixivBrowser = caller.__br__
    db = caller.__dbManager__

    include_show = bookmark_flag in ("n", "y", None, "")
    include_hide = bookmark_flag in ("y", "o")

    online_ids: Set[int] = set()
    try:
        if include_show:
            online_ids.update(_fetch_following_user_ids(br, rest="show"))
        if include_hide:
            online_ids.update(_fetch_following_user_ids(br, rest="hide"))
    except PixivException:
        raise
    except BaseException as ex:
        raise PixivException(f"Failed to fetch following list: {ex}", errorCode=PixivException.SERVER_ERROR)

    stored_ids = set(db.selectFollowMemberIds())

    new_ids = sorted(online_ids - stored_ids)
    removed_ids = sorted(stored_ids - online_ids)
    keep_ids = sorted(online_ids & stored_ids)

    PixivHelper.print_and_log(
        "info",
        f"Follow sync summary: online={len(online_ids)} stored={len(stored_ids)} new={len(new_ids)} removed={len(removed_ids)}",
    )

    preview_root = _build_preview_dir(config.rootDirectory)
    for member_id in removed_ids:
        PixivHelper.print_and_log("info", f"Removing unfollowed member: {member_id}")
        db.deleteFollowMemberCascade(member_id)
        member_preview_dir = os.path.join(preview_root, str(member_id))
        if os.path.isdir(member_preview_dir):
            try:
                shutil.rmtree(member_preview_dir)
            except OSError:
                PixivHelper.print_and_log("warn", f"Failed to remove preview folder: {member_preview_dir}")

    targets: Sequence[Tuple[int, bool]] = [(mid, True) for mid in new_ids] + [(mid, False) for mid in keep_ids]
    for idx, (member_id, is_new) in enumerate(targets, start=1):
        try:
            PixivHelper.print_and_log("info", f"[{idx}/{len(targets)}] Sync member: {member_id} (new={is_new})")
            artist, current_image_ids = _get_member_profile_all(br, int(member_id))
            db.upsertFollowMember(
                member_id=int(member_id),
                name=artist.artistName,
                member_token=artist.artistToken,
                avatar_url=artist.artistAvatar,
                background_url=artist.artistBackground,
            )

            stored_image_ids = set(db.selectFollowImageIdsByMember(member_id))
            current_set = set(int(x) for x in current_image_ids)
            to_add = sorted(current_set - stored_image_ids, reverse=True)
            to_remove = sorted(stored_image_ids - current_set)

            if to_remove:
                PixivHelper.print_and_log("info", f"Member {member_id}: removing {len(to_remove)} deleted works")
                for image_id in to_remove:
                    db.deleteFollowImage(image_id)

            # Resume support: fix images that exist in DB but are missing URL rows (e.g. previous run interrupted).
            try:
                to_fix = db.selectFollowImageIdsNeedingUrlIndex(member_id)
            except Exception:
                to_fix = []
            if to_fix:
                to_fix = [int(x) for x in to_fix if int(x) in current_set]

            to_process: List[int] = list(to_add)
            for image_id in to_fix:
                if image_id not in current_set:
                    continue
                if image_id in stored_image_ids and image_id not in to_process:
                    to_process.append(image_id)

            PixivHelper.print_and_log(
                "info",
                f"Member {member_id}: to_process={len(to_process)} (new={len(to_add)} repair={len(to_fix)})",
            )

            member_preview_dir = os.path.join(preview_root, str(member_id))
            try:
                existing_previews = 0
                if os.path.isdir(member_preview_dir):
                    existing_previews = len([f for f in os.listdir(member_preview_dir) if os.path.isfile(os.path.join(member_preview_dir, f))])
            except OSError:
                existing_previews = 0
            preview_left = max(0, int(preview_per_artist) - existing_previews)

            processed = 0
            total = len(to_process)
            for image_id in to_process:
                try:
                    processed += 1
                    if processed == 1 or processed == total or processed % 10 == 0:
                        PixivHelper.print_and_log("info", f"Member {member_id}: progress {processed}/{total} (image_id={image_id})")

                    image, _ = br.getImagePage(
                        image_id=image_id,
                        parent=artist,
                        from_bookmark=False,
                        skip_medium_page=True,
                    )

                    db.upsertFollowImage(
                        image_id=image.imageId,
                        member_id=member_id,
                        title=image.imageTitle,
                        caption=image.imageCaption,
                        create_date=image.js_createDate,
                        page_count=image.imageCount,
                        mode=image.imageMode,
                        bookmark_count=image.bookmark_count,
                        like_count=image.jd_rtc,
                        view_count=image.jd_rtv,
                    )

                    url_rows = []
                    for page_index, (ori_url, reg_url) in enumerate(zip(image.imageUrls, image.imageResizedUrls)):
                        url_rows.append((image.imageId, page_index, ori_url, reg_url))
                    if url_rows:
                        db.upsertFollowImageUrls(image.imageId, url_rows)

                    # Preview download (keep at most N per artist).
                    if preview_left > 0 and image.imageMode != "ugoira_view":
                        try:
                            preview_url = image.imageResizedUrls[0] if image.imageResizedUrls else None
                            if preview_url:
                                _download_preview_image(
                                    caller=caller,
                                    member_id=member_id,
                                    image_id=image.imageId,
                                    page_index=0,
                                    url=preview_url,
                                )
                                preview_left -= 1
                        except Exception:
                            PixivHelper.print_and_log("warn", f"Preview download failed for image_id={image.imageId}")

                    # Throttle requests (avoid bans), but don't spam logs with "Wait for ..." per item.
                    if getattr(config, "downloadDelay", 0) > 0:
                        time.sleep(random.random() * float(getattr(config, "downloadDelay", 0)))
                except PixivException as ex:
                    PixivHelper.print_and_log("warn", f"Skip image_id={image_id}: {ex}")
                    continue
                except BaseException:
                    PixivHelper.print_and_log("error", f"Error indexing image_id={image_id}: {sys.exc_info()}")
                    continue

            if preview_left > 0:
                PixivHelper.print_and_log("info", f"Member {member_id}: missing {preview_left} preview(s), try to download from indexed urls")
                for image_id in sorted(current_set, reverse=True):
                    if preview_left <= 0:
                        break
                    try:
                        url_rows = db.selectFollowImageUrls(image_id)
                        if not url_rows:
                            continue
                        # (page_index, original_url, regular_url)
                        page0 = next((r for r in url_rows if int(r[0]) == 0), url_rows[0])
                        preview_url = page0[2] or page0[1]
                        if not preview_url or preview_url.endswith(".zip"):
                            continue
                        _download_preview_image(
                            caller=caller,
                            member_id=member_id,
                            image_id=int(image_id),
                            page_index=0,
                            url=preview_url,
                        )
                        preview_left -= 1
                    except Exception:
                        continue

            PixivHelper.print_and_log("info", f"Member {member_id}: sync done.")
        except PixivException as ex:
            PixivHelper.print_and_log("warn", f"Skip member_id={member_id}: {ex}")
        except BaseException:
            PixivHelper.print_and_log("error", f"Error syncing member_id={member_id}: {sys.exc_info()}")
