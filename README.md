# Strava → Garmin Connect sync

Copies title, description and photos from Strava activities onto the matching
Garmin Connect activities for a given date range. Activities are matched by
comparing local start times (Strava and Garmin have no shared activity ID),
so run a dry run first and check the plan before applying.

Photo/description writes use Garmin's undocumented internal API (the same
endpoints the Garmin Connect web/app use) since there's no public API for
this. It can break if Garmin changes their backend.

## Setup

```
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

- `GARMIN_EMAIL` / `GARMIN_PASSWORD` — your normal Garmin Connect login. If
  your account has 2FA enabled, the script will prompt you for the code on
  first run, then cache the session in `~/.garmin_tokens` so you won't need
  to log in (or re-enter MFA) every time.
- `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` / `STRAVA_REFRESH_TOKEN` — see below.

### Getting Strava credentials

1. Go to https://www.strava.com/settings/api and create an API application.
   Note the **Client ID** and **Client Secret**.
2. Authorize your own app for read access to activities (one-time, in a
   browser) — replace `YOUR_CLIENT_ID` below and open the URL:

   ```
   https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=activity:read_all
   ```

3. After approving, you'll be redirected to `http://localhost/?code=...&scope=...`
   (the page won't load, that's expected — copy the `code` value from the URL).
4. Exchange that code for tokens:

   ```
   curl -X POST https://www.strava.com/oauth/token \
     -d client_id=YOUR_CLIENT_ID \
     -d client_secret=YOUR_CLIENT_SECRET \
     -d code=THE_CODE_FROM_STEP_3 \
     -d grant_type=authorization_code
   ```

5. The response includes a `refresh_token` — put that in `.env` as
   `STRAVA_REFRESH_TOKEN`. It doesn't expire from normal use, so this is a
   one-time setup.

## Usage

```
# Preview only — nothing is changed on Garmin (default)
python strava_to_garmin_sync.py --from 2026-06-01 --to 2026-06-30

# Actually apply the changes
python strava_to_garmin_sync.py --from 2026-06-01 --to 2026-06-30 --dry-run no

# Only sync titles + descriptions, skip photos
python strava_to_garmin_sync.py --from 2026-06-01 --to 2026-06-30 --no-photos
```

Every run — dry run or not — prints, for each matched pair, the Garmin
activity it will touch and exactly what will be written (new title,
description preview, photo count). Unmatched Strava activities (no Garmin
activity found within the matching window) are listed separately at the end.

### Options

| Flag | Default | Description |
|---|---|---|
| `--from` / `--to` | required | Date range (YYYY-MM-DD) of Strava activities to sync |
| `--dry-run` | `yes` | `yes` = preview only, `no` = apply changes |
| `--match-window` | `8` | Minutes of tolerance when matching Strava/Garmin activities by start time |
| `--max-photos` | `10` | Max photos synced per activity |
| `--no-title` / `--no-description` / `--no-photos` | off | Skip syncing that field |
| `--garmin-tokenstore` | `~/.garmin_tokens` | Where the Garmin login session is cached |
| `--env-file` | `.env` | Path to the credentials file |

## Notes / limitations

- Matching is purely time-based (Strava and Garmin local start times within
  `--match-window` minutes of each other). If two activities on the same day
  start close together, double check the printed plan before running live.
- Strava's activity list endpoint doesn't include descriptions, so the
  script fetches full detail + photos only for activities it has matched
  (keeps API calls low while still being accurate).
- Rate limits: Strava allows 100 requests/15min, 1000/day by default; Garmin
  has no published limit but throttles aggressively — avoid syncing huge
  date ranges in one run.
