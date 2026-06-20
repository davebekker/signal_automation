# Signal Automation Bot

> Replace placeholders such as `<user>`, `<uid>`, `<google-workspace-or-domain>`, `<google-user>`, and `<camera-archive-folder>` with values from the target machine. Do not commit real API keys, Signal state, or private config files to a public repository.

A personal Signal-based automation hub that routes slash commands from Signal to a set of small Python bots. The project currently runs on an Ubuntu desktop, with the Signal API served by the `bbernhard/signal-cli-rest-api` Docker image and the Python master bot managed by a `systemd --user` service.

The design goal is pragmatic reliability: keep the home network infrastructure separate from the bot stack, run the bot automatically after reboot, and keep enough config/state backed up to recover quickly after SSD failure or reinstall.

---

## High-level architecture

```text
Signal app
   |
   | Signal messages / slash commands
   v
signal-cli-rest-api Docker container
   |
   | HTTP + WebSocket API on localhost:8080
   v
master_bot_v2.py
   |
   | route by sender / recipient / command context
   v
individual bots
   |-- reminders
   |-- budget
   |-- bins
   |-- trains
   |-- Nest cameras
   |-- Bluesky
```

The master bot owns the Signal integration and routing. Individual bots own their own command parsing, state, and external integrations. This keeps the Signal plumbing in one place while letting each bot evolve independently.

---

## Main components

### `master_bot_v2.py`

The main orchestrator.

Responsibilities:

- Connects to `signal-cli-rest-api`.
- Listens for incoming Signal messages.
- Routes messages to the correct bot.
- Sends replies back through Signal.
- Supervises background tasks.
- Maintains the Signal-to-Bluesky bridge store.
- Handles safer attachment download/send behaviour.

The current preferred version has Bluesky background sync disabled by default. Bluesky commands still work, but notification polling does not start automatically unless explicitly enabled.

Relevant setting:

```bash
BLUESKY_AUTO_SYNC_ENABLED=0
```

Set to `1` only if you want automatic Bluesky notification/reply syncing.

---

### `bots/reminder_bot.py`

Reminder and recurring-plan bot.

Features:

- One-off reminders.
- Recurring reminders.
- Birthdays.
- Check-ins.
- Flower reminders.
- Cleaner `/list` output.
- `/confirm` and `/cancel` flow for ambiguous natural-language reminders.
- Optional Gemini Flash parser for natural-language reminder creation.

Typical commands:

```text
/remind me to renew my passport in three months
/remind tomorrow 09:00 | submit expenses
/recur every Friday 09:00 | review budget
/list
/plans
/confirm abc123
/cancel abc123
/del 4
```

Config file:

```text
config/reminder_bot.json
```

Example:

```json
{
  "llm_enabled": true,
  "gemini_api_key": "paste-your-key-here",
  "llm_model": "gemini-2.5-flash",
  "llm_timeout_seconds": 8,
  "llm_auto_confirm_threshold": 0.75,
  "llm_confirmation_threshold": 0.45,
  "confirmation_expiry_minutes": 30,
  "db_path": "data/reminders.sqlite"
}
```

The LLM only parses user intent into structured reminder fields. The bot still validates the result and owns all scheduling/state writes.

---

### `budget_bot.py`

Small personal budget tracker.

Features:

- Balance tracking.
- Weekly allowance.
- Safer decimal currency parsing.
- Atomic JSON state writes.
- `/undo` for the last transaction.
- Cleaner `/history` output.
- Config file support.

Typical commands:

```text
/balance
/add 10 refund
/sub £4.50 coffee
/history
/history 30
/undo
/setbalance 50
```

Config file:

```text
config/budget_bot.json
```

Example:

```json
{
  "state_file": "budget_state.json",
  "currency_symbol": "£",
  "weekly_amount": 1.0,
  "max_history": 100,
  "history_display_limit": 20,
  "allow_negative_balance": true
}
```

State file:

```text
budget_state.json
```

This file is important and should be backed up.

---

### `nest_bot.py`

Nest camera sync/archive bot.

Features:

- Syncs Nest camera events.
- Downloads camera clips.
- Uses a RAM-backed working directory when configured, to reduce SSD writes.
- Archives clips to Google Drive when GVFS Drive is available.
- Stages clips locally if Drive is unavailable.
- Retries staged clips later.
- Adds `/nest status` and `/nest flush`.

Recommended config:

```text
config/nest_bot.json
```

Example for the current desktop setup:

```json
{
  "sync_interval_minutes": 30,
  "messaging_enabled": false,
  "state_file": "nest_state.json",
  "download_path": "/dev/shm/nest_events",
  "pending_archive_dir": "data/nest_pending_archive",
  "drive_base": "/run/user/<uid>/gvfs/google-drive:host=<google-workspace-or-domain>,user=<google-user>",
  "drive_archive_path": "/run/user/<uid>/gvfs/google-drive:host=<google-workspace-or-domain>,user=<google-user>/My Drive/<camera-archive-folder>",
  "mount_drive": "google-drive://<google-workspace-or-domain>/<google-user>",
  "monitored_cameras": ["Backyard", "Nest Doorbell (battery)"],
  "ignored_cameras": ["Rookery"],
  "lookback_cap_minutes": 150,
  "first_sync_lookback_minutes": 180,
  "sync_overlap_minutes": 2,
  "max_folder_gb": 3,
  "max_age_days": 30,
  "recent_events_limit": 50,
  "pending_retry_limit": 25,
  "delete_local_after_drive_copy": true
}
```

The normal SSD-saving flow is:

```text
/dev/shm/nest_events -> Google Drive -> delete local working file
```

The fallback flow is:

```text
/dev/shm/nest_events -> data/nest_pending_archive -> Google Drive later
```

`data/nest_pending_archive` should be backed up because it may contain clips that have not reached Drive yet.

---

### `bots/bluesky_bot.py`

Bluesky posting/reply helper.

Features:

- Posts to Bluesky.
- Handles replies and bridging behaviour.
- Can use image attachments.
- Background notification sync is now disabled by default at the master level.

Useful operational note:

- Keep manual Bluesky commands enabled.
- Only enable background sync when you explicitly want continuous notification polling.

---

### `bin_bot.py`

Council bin collection helper.

Current role:

- Fetches/scrapes bin collection information.
- Sends collection reminders.

Potential future improvements:

- Config file for council URL and reminder times.
- Cache freshness reporting.
- `/bins refresh` command.

---

### `train_bot.py`

Train information/watch bot.

Current role:

- Fetches train running information.
- Supports train watches.

Potential future improvements:

- Persist active watches across restart.
- Make `/watch` route-explicit.
- Add `/watching` and `/unwatch` list-based management.

---

### `utils/`

Shared support code, currently including Nest authentication/API helpers and related models/tools.

Important files observed in the project:

```text
utils/google_auth_wrapper.py
utils/models.py
utils/nest_api.py
utils/tools.py
```

---

## Local directory layout

Current expected working directory:

```text
/home/<user>/Projects/signal_automation
```

Suggested layout:

```text
signal_automation/
├── master_bot_v2.py
├── budget_bot.py
├── bin_bot.py
├── train_bot.py
├── nest_bot.py
├── bots/
│   ├── reminder_bot.py
│   └── bluesky_bot.py
├── utils/
├── config/
│   ├── reminder_bot.json
│   ├── budget_bot.json
│   └── nest_bot.json
├── data/
│   ├── reminders.sqlite
│   ├── bridge_store.sqlite
│   └── nest_pending_archive/
├── scripts/
│   ├── backup-to-google-drive.sh
│   └── restore-signal-automation.sh
├── budget_state.json
├── nest_state.json
└── README.md
```

Signal API state lives outside the project folder:

```text
/home/<user>/signal-data
```

This is critical state. Back it up.

---

## Ubuntu setup from scratch

### 1. Install baseline packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git rsync curl
```

Docker is currently installed via Snap on this machine, so Docker commands use:

```bash
/snap/bin/docker
```

Check Docker:

```bash
/snap/bin/docker ps
```

If this only works with `sudo`, fix Docker permissions or use system-level services instead of `systemd --user`.

---

### 2. Restore or clone project

Expected location:

```bash
mkdir -p /home/<user>/Projects
cd /home/<user>/Projects
```

Restore the project into:

```text
/home/<user>/Projects/signal_automation
```

Create virtual environment:

```bash
cd /home/<user>/Projects/signal_automation
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
```

Install dependencies according to the project requirements. If `requirements.txt` exists:

```bash
pip install -r requirements.txt
```

If not, install the known runtime dependencies used by the bots, adjusting as needed:

```bash
pip install aiohttp websockets httpx dateparser google-genai python-dateutil
```

Nest/Bluesky dependencies may require additional packages depending on the exact current code.

---

### 3. Restore config and state

Restore these from backup if available:

```text
config/*.json
budget_state.json
nest_state.json
data/reminders.sqlite*
data/bridge_store.sqlite*
data/nest_pending_archive/
```

SQLite WAL files matter if present:

```text
*.sqlite
*.sqlite-wal
*.sqlite-shm
```

Restore Signal data:

```text
/home/<user>/signal-data
```

---

## Signal API container

The Signal API container is run directly by Docker, not by systemd. Docker's restart policy keeps it alive across reboot.

Start/recreate container:

```bash
/snap/bin/docker rm -f signal-api 2>/dev/null || true

/snap/bin/docker run -d --name signal-api --restart=unless-stopped \
  -p 8080:8080 \
  -v /home/<user>/signal-data:/home/.local/share/signal-cli \
  -e MODE=json-rpc-native \
  -e JSON_RPC_TRUST_NEW_IDENTITIES=always \
  bbernhard/signal-cli-rest-api:latest
```

Check container:

```bash
/snap/bin/docker ps | grep signal-api
/snap/bin/docker logs -n 100 signal-api
```

---

## systemd user service for the master bot

The Python master bot is managed by `systemd --user`.

Service file:

```text
/home/<user>/.config/systemd/user/signal-master-bot.service
```

Recommended content:

```ini
[Unit]
Description=Signal Master Bot

[Service]
Type=simple
WorkingDirectory=/home/<user>/Projects/signal_automation
ExecStartPre=/bin/sleep 20
ExecStart=/home/<user>/Projects/signal_automation/.venv/bin/python /home/<user>/Projects/signal_automation/master_bot_v2.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

Enable lingering so the user service starts after reboot before SSH login:

```bash
sudo loginctl enable-linger <user>
loginctl show-user <user> | grep Linger
```

Expected:

```text
Linger=yes
```

Enable and start service:

```bash
systemctl --user daemon-reload
systemctl --user enable signal-master-bot.service
systemctl --user start signal-master-bot.service
```

Check status:

```bash
systemctl --user --no-pager status signal-master-bot.service
```

Logs:

```bash
journalctl --user -u signal-master-bot.service -f
journalctl --user -u signal-master-bot.service -b -n 100 --no-pager
```

Restart:

```bash
systemctl --user restart signal-master-bot.service
```

Stop:

```bash
systemctl --user stop signal-master-bot.service
```

---

## Google Drive and Nest caveat

The current Google Drive path is a GNOME/GVFS mount:

```text
/run/user/<uid>/gvfs/google-drive:host=<google-workspace-or-domain>,user=<google-user>/My Drive/<camera-archive-folder>
```

This path usually exists only after the desktop GUI session has logged in and Drive has mounted.

Before login:

- The master bot can run.
- Nest can sync.
- Drive archiving may be unavailable.
- Clips should stage in `data/nest_pending_archive`.

After login:

- GVFS Drive appears.
- Nest can flush staged clips to Drive.

Useful checks:

```bash
ls -ld "/run/user/<uid>/gvfs/google-drive:host=<google-workspace-or-domain>,user=<google-user>/My Drive/<camera-archive-folder>"
journalctl --user -u signal-master-bot.service -b --no-pager | grep -i "nest\|drive\|archive\|pending"
```

Signal commands:

```text
/nest status
/nest flush
```

---

## Backup to Google Drive

Google Drive backup folder:

```text
/run/user/<uid>/gvfs/google-drive:host=<google-workspace-or-domain>,user=<google-user>/My Drive/signal_automation
```

Recommended backup layout:

```text
signal_automation/
├── latest/
│   ├── project/
│   └── signal-data/
└── snapshots/
    └── YYYYMMDD_HHMMSS/
        ├── project/
        └── signal-data/
```

Suggested backup script:

```text
scripts/backup-to-google-drive.sh
```

Because GVFS Google Drive does not behave like a full POSIX filesystem, avoid `rsync -a`. Use options like:

```bash
rsync -rv --delete --no-perms --no-owner --no-group --omit-dir-times ...
```

Important backup contents:

```text
project code
config/*.json
budget_state.json
nest_state.json
data/*.sqlite*
data/nest_pending_archive/
Signal API state directory
README/runbook/restore scripts
```

Avoid or exclude:

```text
.venv/
__pycache__/
*.pyc
.git/
logs/
tmp/
data/nest_clips/
downloads/
backups/
```

---

## Restore from Google Drive

Restore script:

```text
scripts/restore-signal-automation.sh
```

Default dry run:

```bash
./scripts/restore-signal-automation.sh
```

Apply latest restore:

```bash
./scripts/restore-signal-automation.sh --apply
```

Apply a specific snapshot:

```bash
./scripts/restore-signal-automation.sh --source snapshots/20260620_153053 --apply
```

After restore:

```bash
/snap/bin/docker ps | grep signal-api
systemctl --user --no-pager status signal-master-bot.service
journalctl --user -u signal-master-bot.service -n 100 --no-pager
```

---

## Updating Signal CLI / Signal API image

The project uses:

```text
bbernhard/signal-cli-rest-api:latest
```

The container's restart policy keeps it running after reboot, but it does not automatically pull new images. Updating Signal CLI means pulling the latest image and recreating the container with the same persistent volume.

Recommended manual update process:

```bash
# 1. Back up Signal state
mkdir -p /home/<user>/backups/signal_api
rsync -a /home/<user>/signal-data/ \
  /home/<user>/backups/signal_api/signal-data-$(date +%Y%m%d_%H%M%S)/

# 2. Pull latest image
/snap/bin/docker pull bbernhard/signal-cli-rest-api:latest

# 3. Recreate container
/snap/bin/docker rm -f signal-api

/snap/bin/docker run -d --name signal-api --restart=unless-stopped \
  -p 8080:8080 \
  -v /home/<user>/signal-data:/home/.local/share/signal-cli \
  -e MODE=json-rpc-native \
  -e JSON_RPC_TRUST_NEW_IDENTITIES=always \
  bbernhard/signal-cli-rest-api:latest

# 4. Restart and check bot
systemctl --user restart signal-master-bot.service
journalctl --user -u signal-master-bot.service -n 100 --no-pager
```

Manual updates are preferred over automatic updates because Signal is stateful. Back up before updating.

---

## Common operational commands

Check bot:

```bash
systemctl --user --no-pager status signal-master-bot.service
journalctl --user -u signal-master-bot.service -f
```

Restart bot:

```bash
systemctl --user restart signal-master-bot.service
```

Check Signal API:

```bash
/snap/bin/docker ps | grep signal-api
/snap/bin/docker logs -n 100 signal-api
```

Restart Signal API manually:

```bash
/snap/bin/docker restart signal-api
systemctl --user restart signal-master-bot.service
```

Check current boot logs:

```bash
journalctl --user -u signal-master-bot.service -b -n 200 --no-pager
```

Search logs:

```bash
journalctl --user -u signal-master-bot.service -b --no-pager | grep -i "error\|warning\|nest\|drive\|bluesky"
```

---

## Recovery checklist after SSD failure / reinstall

1. Install Ubuntu and baseline packages.
2. Install or restore Docker/Snap Docker.
3. Restore project folder to:

   ```text
   /home/<user>/Projects/signal_automation
   ```

4. Restore Signal data to:

   ```text
   /home/<user>/signal-data
   ```

5. Recreate Python virtual environment.
6. Install Python dependencies.
7. Restore `config/*.json` and state files.
8. Start Signal API container.
9. Create/enable `signal-master-bot.service`.
10. Enable linger.
11. Start service and check logs.
12. Log into desktop once to confirm Google Drive/GVFS mount.
13. Run `/nest status` and `/nest flush` from Signal.
14. Run backup script once to confirm Drive backups work.

---

## Security notes

- Config files may contain API keys and tokens.
- Signal state contains sensitive account/session data.
- Google Drive backups should be treated as sensitive.
- Avoid sharing config/state publicly.
- Do not commit real secrets to a public Git repository.

---

## Current design choices

- Run the bot on a non-critical host where possible, rather than on DNS/VPN/router infrastructure, to reduce blast radius.
- Use Docker restart policy for Signal API.
- Use `systemd --user` for the Python master bot.
- Keep Google Drive as backup/recovery storage, not the live working directory.
- Use local SSD for durable state, RAM drive for Nest temporary clips, and Google Drive for archive/backup.
- Prefer manual Signal API image updates with backups over automatic updates.
