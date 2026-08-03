#!/usr/bin/env python3
"""
strava_to_garmin_sync.py

Copies the title, description and photos of Strava activities onto the
matching Garmin Connect activities, for a given date range.

There is no shared ID between a Strava activity and its Garmin twin, so
activities are matched by comparing local start times (within a tolerance
window). Always run with --dry-run yes first and check the printed plan
before applying changes.

Setup
-----
1. pip install -r requirements.txt
2. Create a .env file (see .env.example) with:
     STRAVA_CLIENT_ID
     STRAVA_CLIENT_SECRET
     STRAVA_REFRESH_TOKEN
     GARMIN_EMAIL
     GARMIN_PASSWORD
   See README.md for how to obtain the Strava values.

Usage
-----
  # Preview only (default, nothing is changed on Garmin)
  python strava_to_garmin_sync.py --from 2026-06-01 --to 2026-06-30

  # Actually apply the changes
  python strava_to_garmin_sync.py --from 2026-06-01 --to 2026-06-30 --dry-run no

  # Only sync titles + descriptions, skip photos
  python strava_to_garmin_sync.py --from 2026-06-01 --to 2026-06-30 --no-photos
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import requests

try:
    from stravalib.client import Client as StravaClient
except ImportError:
    print("Missing dependency: pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    import garminconnect
except ImportError:
    print("Missing dependency: pip install -r requirements.txt", file=sys.stderr)
    raise


# --------------------------------------------------------------------------
# .env loading (no external dependency required)
# --------------------------------------------------------------------------

def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (without
    overwriting variables already set in the real environment)."""
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------

@dataclass
class GarminActivity:
    id: str
    name: str
    start_local: datetime
    raw: dict[str, Any]


@dataclass
class StravaActivity:
    id: int
    name: str
    start_local: datetime
    summary: Any  # stravalib SummaryActivity


@dataclass
class SyncPlan:
    garmin: GarminActivity
    strava: StravaActivity
    new_title: Optional[str] = None
    new_description: Optional[str] = None
    photo_urls: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.new_title or self.new_description or self.photo_urls)


# --------------------------------------------------------------------------
# Strava
# --------------------------------------------------------------------------

def strava_login() -> StravaClient:
    client_id = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN")
    missing = [
        name
        for name, val in [
            ("STRAVA_CLIENT_ID", client_id),
            ("STRAVA_CLIENT_SECRET", client_secret),
            ("STRAVA_REFRESH_TOKEN", refresh_token),
        ]
        if not val
    ]
    if missing:
        raise SystemExit(
            f"Missing Strava credentials in environment: {', '.join(missing)}. "
            "See README.md for how to obtain them."
        )

    client = StravaClient()
    # refresh_access_token() also sets client.access_token internally;
    # it returns a dict with access_token/refresh_token/expires_at.
    client.refresh_access_token(
        client_id=int(client_id),
        client_secret=client_secret,
        refresh_token=refresh_token,
    )
    return client


def fetch_strava_activities(
    client: StravaClient, date_from: datetime, date_to: datetime
) -> list[StravaActivity]:
    # `before` is exclusive-ish in practice, so push it to the end of the day.
    before = date_to + timedelta(days=1)
    results = []
    for summary in client.get_activities(after=date_from, before=before):
        start_local = summary.start_date_local
        if start_local is None:
            continue
        # stravalib returns naive local datetimes for start_date_local
        results.append(
            StravaActivity(
                id=summary.id,
                name=summary.name or "",
                start_local=start_local.replace(tzinfo=None),
                summary=summary,
            )
        )
    return results


def fetch_strava_detail_and_photos(
    client: StravaClient, activity_id: int, max_photos: int
) -> tuple[str, list[str]]:
    """Returns (description, photo_urls)."""
    description = ""
    try:
        detail = client.get_activity(activity_id)
        description = detail.description or ""
    except Exception as exc:  # noqa: BLE001
        print(f"  ! Could not fetch full detail for Strava activity {activity_id}: {exc}")

    photo_urls: list[str] = []
    try:
        photos = client.get_activity_photos(activity_id, size=2000)
        for photo in photos:
            if not photo.urls:
                continue
            # urls is a dict keyed by requested size; take the highest-res one available
            best_url = sorted(photo.urls.items(), key=lambda kv: _safe_int(kv[0]))[-1][1]
            photo_urls.append(best_url)
            if len(photo_urls) >= max_photos:
                break
    except Exception as exc:  # noqa: BLE001
        print(f"  ! Could not fetch photos for Strava activity {activity_id}: {exc}")

    return description, photo_urls


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# Garmin
# --------------------------------------------------------------------------

def garmin_login(tokenstore: str) -> "garminconnect.Garmin":
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        raise SystemExit(
            "Missing GARMIN_EMAIL / GARMIN_PASSWORD in environment. See README.md."
        )

    garmin = garminconnect.Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Enter Garmin Connect MFA code: "),
    )
    garmin.login(tokenstore)
    return garmin


def fetch_garmin_activities(
    garmin: "garminconnect.Garmin", date_from: datetime, date_to: datetime
) -> list[GarminActivity]:
    # Widen by a day on each side to absorb local/UTC boundary edge cases;
    # the time-window matching step will filter out anything too far off.
    start = (date_from - timedelta(days=1)).strftime("%Y-%m-%d")
    end = (date_to + timedelta(days=1)).strftime("%Y-%m-%d")
    raw_activities = garmin.get_activities_by_date(start, end)

    results = []
    for act in raw_activities:
        start_local_raw = act.get("startTimeLocal")
        if not start_local_raw:
            continue
        try:
            start_local = datetime.strptime(start_local_raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        results.append(
            GarminActivity(
                id=str(act.get("activityId")),
                name=act.get("activityName") or "",
                start_local=start_local,
                raw=act,
            )
        )
    return results


def garmin_set_description(garmin: "garminconnect.Garmin", activity_id: str, description: str) -> Any:
    """Not exposed by python-garminconnect; mirrors its set_activity_name
    implementation (same endpoint, different field)."""
    url = f"{garmin.garmin_connect_activity}/{activity_id}"
    payload = {"activityId": activity_id, "description": description}
    return garmin.client.put("connectapi", url, json=payload, api=True)


def garmin_upload_image(garmin: "garminconnect.Garmin", activity_id: str, image_path: Path) -> Any:
    """Not exposed by python-garminconnect. Uses the same undocumented
    endpoint as Garmin Connect's own web/app clients:
    POST /activity-service/activity/{id}/image (multipart file upload).
    This relies on Garmin's internal API and may break if Garmin changes it."""
    url = f"{garmin.garmin_connect_activity}/{activity_id}/image"
    with open(image_path, "rb") as fh:
        files = {"file": (image_path.name, fh, "application/octet-stream")}
        return garmin.client.post("connectapi", url, files=files, api=True)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def match_activities(
    strava_activities: list[StravaActivity],
    garmin_activities: list[GarminActivity],
    window_minutes: int,
) -> tuple[list[tuple[StravaActivity, GarminActivity]], list[StravaActivity]]:
    candidates = []
    for s in strava_activities:
        for g in garmin_activities:
            diff = abs((s.start_local - g.start_local).total_seconds()) / 60.0
            if diff <= window_minutes:
                candidates.append((diff, s, g))

    candidates.sort(key=lambda c: c[0])

    used_strava: set[int] = set()
    used_garmin: set[str] = set()
    matches: list[tuple[StravaActivity, GarminActivity]] = []

    for _diff, s, g in candidates:
        if s.id in used_strava or g.id in used_garmin:
            continue
        used_strava.add(s.id)
        used_garmin.add(g.id)
        matches.append((s, g))

    unmatched = [s for s in strava_activities if s.id not in used_strava]
    # keep chronological order for readability
    matches.sort(key=lambda pair: pair[0].start_local)
    unmatched.sort(key=lambda s: s.start_local)
    return matches, unmatched


# --------------------------------------------------------------------------
# Plan building / printing / applying
# --------------------------------------------------------------------------

def build_plan(
    strava_client: StravaClient,
    strava: StravaActivity,
    garmin: GarminActivity,
    args: argparse.Namespace,
) -> SyncPlan:
    plan = SyncPlan(garmin=garmin, strava=strava)

    if not args.no_title and strava.name and strava.name != garmin.name:
        plan.new_title = strava.name

    if not args.no_description or not args.no_photos:
        description, photo_urls = fetch_strava_detail_and_photos(
            strava_client, strava.id, args.max_photos
        )
        if not args.no_description and description:
            plan.new_description = description
        if not args.no_photos:
            plan.photo_urls = photo_urls

    return plan


def print_plan(plan: SyncPlan) -> None:
    s, g = plan.strava, plan.garmin
    print(f"\nGarmin activity {g.id} — \"{g.name}\" ({g.start_local})")
    print(f"  matched Strava activity {s.id} — \"{s.name}\" ({s.start_local})")
    if plan.new_title:
        print(f"  title:       \"{g.name}\" -> \"{plan.new_title}\"")
    if plan.new_description:
        snippet = plan.new_description.replace("\n", " ")[:120]
        print(f"  description: -> \"{snippet}{'...' if len(plan.new_description) > 120 else ''}\"")
    if plan.photo_urls:
        print(f"  photos:      {len(plan.photo_urls)} photo(s) to upload")
    if not plan.has_changes:
        print("  (nothing to update)")


def download_photo(url: str, dest_dir: Path) -> Optional[Path]:
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"    ! Failed to download photo: {exc}")
        return None
    ext = ".jpg"
    content_type = resp.headers.get("Content-Type", "")
    if "png" in content_type:
        ext = ".png"
    dest = dest_dir / f"photo_{abs(hash(url))}{ext}"
    dest.write_bytes(resp.content)
    return dest


def apply_plan(garmin: "garminconnect.Garmin", plan: SyncPlan) -> None:
    g = plan.garmin

    if plan.new_title:
        try:
            garmin.set_activity_name(g.id, plan.new_title)
            print(f"  applied title")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Failed to set title: {exc}")

    if plan.new_description:
        try:
            garmin_set_description(garmin, g.id, plan.new_description)
            print(f"  applied description")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Failed to set description: {exc}")

    if plan.photo_urls:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            uploaded = 0
            for url in plan.photo_urls:
                photo_path = download_photo(url, tmp_path)
                if photo_path is None:
                    continue
                try:
                    garmin_upload_image(garmin, g.id, photo_path)
                    uploaded += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! Failed to upload photo: {exc}")
            print(f"  applied {uploaded}/{len(plan.photo_urls)} photo(s)")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date '{value}', expected YYYY-MM-DD") from exc


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="date_from", type=parse_date, required=True, help="Start date (YYYY-MM-DD), Strava activities from this date")
    parser.add_argument("--to", dest="date_to", type=parse_date, required=True, help="End date (YYYY-MM-DD), inclusive")
    parser.add_argument("--dry-run", choices=["yes", "no"], default="yes", help="yes (default): only show what would change. no: apply changes to Garmin.")
    parser.add_argument("--match-window", type=int, default=8, help="Minutes of tolerance when matching Strava/Garmin activities by start time (default: 8)")
    parser.add_argument("--max-photos", type=int, default=10, help="Max photos to sync per activity (default: 10)")
    parser.add_argument("--no-title", action="store_true", help="Don't sync activity titles")
    parser.add_argument("--no-description", action="store_true", help="Don't sync activity descriptions")
    parser.add_argument("--no-photos", action="store_true", help="Don't sync photos")
    parser.add_argument("--garmin-tokenstore", default=str(Path.home() / ".garmin_tokens"), help="Where to cache Garmin login session (default: ~/.garmin_tokens)")
    parser.add_argument("--env-file", default=".env", help="Path to a .env file with credentials (default: ./.env)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file))

    if args.date_from > args.date_to:
        print("--from must be before or equal to --to", file=sys.stderr)
        return 1

    print(f"Fetching Strava activities from {args.date_from.date()} to {args.date_to.date()}...")
    strava_client = strava_login()
    strava_activities = fetch_strava_activities(strava_client, args.date_from, args.date_to)
    print(f"Found {len(strava_activities)} Strava activities.")

    print("Logging in to Garmin Connect...")
    garmin = garmin_login(args.garmin_tokenstore)
    garmin_activities = fetch_garmin_activities(garmin, args.date_from, args.date_to)
    print(f"Found {len(garmin_activities)} Garmin activities in range (with buffer).")

    matches, unmatched = match_activities(strava_activities, garmin_activities, args.match_window)

    if args.dry_run == "yes":
        print("\n=== DRY RUN — no changes will be made to Garmin Connect ===")
    else:
        print("\n=== LIVE RUN — Garmin Connect activities will be updated ===")

    updated = 0
    skipped_no_change = 0
    errors = 0

    for strava, garmin_act in matches:
        try:
            plan = build_plan(strava_client, strava, garmin_act, args)
        except Exception as exc:  # noqa: BLE001
            print(f"\n! Error building plan for Strava activity {strava.id}: {exc}")
            errors += 1
            continue

        print_plan(plan)

        if not plan.has_changes:
            skipped_no_change += 1
            continue

        if args.dry_run == "no":
            apply_plan(garmin, plan)
        updated += 1

    if unmatched:
        print(f"\n{len(unmatched)} Strava activities had no matching Garmin activity within "
              f"{args.match_window} minutes and were skipped:")
        for s in unmatched:
            print(f"  - Strava {s.id} \"{s.name}\" ({s.start_local})")

    print(
        f"\nSummary: {len(matches)} matched, {updated} with changes "
        f"({'applied' if args.dry_run == 'no' else 'not applied (dry run)'}), "
        f"{skipped_no_change} already up to date, {errors} errors, "
        f"{len(unmatched)} unmatched."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
