# BDT Desktop App

Cognitive assessment desktop client — block design test with YOLO hand detection, MediaPipe hand tracking, ESP32 physical button support, multiplayer duel mode, and tournament cup mode.

## Tech Stack

| Layer | Technology |
|-------|------------|
| GUI | CustomTkinter (dark theme, responsive layout) |
| ML | YOLOv5 + MediaPipe (hand detection) |
| Camera | OpenCV |
| Audio | numpy + sounddevice (procedural SFX, no .wav files) |
| Serial | pyserial (ESP32 buttons) |
| HTTP | requests (lazy-loaded, fire-and-forget with retry) |
| WebSocket | Custom GameWebSocket client (gorilla/websocket protocol) |

## Prerequisites

- Python 3.9+
- Camera (built-in or USB webcam)
- Optional: ESP32 with buttons (for `BUTTON_MODE`)

## Setup

```bash
python -m venv venv
source venv/bin/activate      # Linux/Mac
venv\Scripts\activate         # Windows

pip install -r requirements.txt

cp .env.example .env
# Edit .env for your setup
```

## Run

```bash
python main.py
```

Window starts maximized with dark theme. Linux uses `attributes('-zoomed')`, others use `state('zoomed')`.

## Configuration (`.env`)

| Var | Values | Default | Effect |
|-----|--------|---------|--------|
| `PC_MODE` | `competition` / `training` | `training` | `competition`: UID required, multiplayer lobby. `training`: skip to game |
| `API_SERVER_URL` | URL or empty | (empty) | Empty = offline solo mode, no server calls |
| `MODEL_BANTAL` | `true` / `false` | `false` | `true`: `MODEL/bantal/bantal.pt`. `false`: `MODEL/exp7/weights/best.pt` |
| `NORMAL_PATTERN` | `true` / `false` | `false` | Switches between two `LEVEL_ANSWERS` dicts and `FILES/` subdirectories |
| `MAX_LEVEL` | 1-8 | `8` | Number of levels per game session |
| `BUTTON_MODE` | `true` / `false` | `false` | Enable ESP32 serial button reader |
| `DISPLAY_HALF` | `true` / `false` | `false` | Half-screen camera layout |
| `HIDE_CAMERA` | `true` / `false` | `false` | Hide camera feed entirely |
| `CUSTOM_LEVEL` | e.g. `1a,2b,3c` | (empty) | Override random level variant selection |
| `CAMERA_INDEX` | integer | `0` | Camera device index for OpenCV |
| `CAMERA_MIRROR_X` | `true` / `false` | `false` | Horizontal flip camera feed |
| `CAMERA_MIRROR_Y` | `true` / `false` | `false` | Vertical flip camera feed |
| `CAMERA_ZOOM` | float 0.5-3.0 | `1.0` | Camera zoom factor |
| `DEBUG_MODE` | `true` / `false` | `false` | Print detailed performance metrics |
| `THEME` | `dark` / `light` | `dark` | UI color theme (requires restart) |
| `YOLO_SKIP_FRAMES` | integer | `2` | YOLO inference every N+1 frames |
| `MEDIAPIPE_SKIP_FRAMES` | integer | `2` | MediaPipe skip count |
| `ROOM_ID` | string | (empty) | Fallback room code (ignored when GUI lobby is used) |
| `PLAYER_NUM` | 1 / 2 | (empty) | Fallback player number |

## Architecture

Single-file app: all logic in `main.py` (~4900 lines). No modules or packages.

### Classes

| Class | Purpose |
|-------|---------|
| `App_Input` | UID verification + camera setup screen with live preview |
| `App_Room` | Multiplayer lobby — create/join room, real-time status, ready check |
| `TimeIn` | Main game window — camera feed, YOLO detection, timer, results dashboard |
| `CameraSettingsDialog` | Camera device, mirror, zoom configuration (saved to .env) |
| `GameWebSocket` | WebSocket client for real-time match events (score, GAME_OVER, match_result) |
| `YOLODetectionThread` | Background YOLO inference daemon with result queue |
| `SerialReaderThread` | ESP32 serial button input reader |

### Startup (critical)

Heavy initialization runs at module level **before** `if __name__`:
1. `python-dotenv` loads `.env`
2. Global env vars parsed (PC_MODE, CAMERA_*, BUTTON_MODE, etc.)
3. `torch` + YOLO model loaded to GPU/CPU
4. All test images read from `FILES/` into memory
5. Audio (sounddevice), OpenCV, MediaPipe, CustomTkinter initialized
6. `LEVEL_ANSWERS` dict selected based on `NORMAL_PATTERN`

**Never `import main` from another file** — the module-level init loads the entire ML pipeline.

### Game Flow

```
┌─────────────────────────────────────────────────────────────────┐
│  Training Mode                    Competition Mode              │
│                                                                   │
│  App_Input (UID optional)         App_Input (UID required)      │
│       │                                 │                         │
│       │ skip lobby                     App_Room (lobby)           │
│       │                                 │                         │
│       ▼                                 ▼                         │
│  TimeIn (game) ◄─────────────── TimeIn (game)                   │
│       │                                 │                         │
│       ▼                                 ▼                         │
│  Results dashboard              Results + duel result             │
│  "Main Lagi" / "Salin Hasil"    "Main Lagi" / "Ganti Peserta"  │
│  / "Ganti Peserta"               / "Salin Hasil"                │
│                                                                   │
│  Tournament Cup Mode:                                            │
│  App_Input → auto-detect match → skip lobby → TimeIn            │
└─────────────────────────────────────────────────────────────────┘
```

### Screen Navigation

- **"✕ Keluar" button** (TimeIn top-right): Returns to App_Input to register a new participant. Resets all game state.
- **"Main Lagi" button** (Results): Replays with the same participant. Preserves UID/name, goes through App_Room if competition.
- **"✕ Ganti Peserta" button** (App_Room header + Results): Goes back to App_Input. Resets all globals.
- **"Keluar Room"** (App_Room lobby): Leaves the room but stays in App_Room.
- **Window X button** (all screens): Properly cleans up YOLO thread, serial thread, WebSocket, camera resources.

## Scoring & Results

### Surrender (Enter key)

Pressing Enter during a level **surrenders** it — the level is recorded as `0.0` time (not completed). This means:
- `level_reached` does NOT count surrendered levels (only `task > 0`)
- No cognitive age contribution for surrendered levels
- A descending "skip" sound effect plays
- The game continues to the next level or ends if all levels are done

### Score Formula (shared with backend)

```
score = (level_reached × 10) + (visuo_spatial_fit × 50) + (dexterity_score × 0.2)
```

- `level_reached`: count of levels with `task_time > 0`
- `visuo_spatial_fit`: `visuo_spatial / 100` (percentage → 0.0-1.0)
- `dexterity_score`: `cognitive_age / real_age` (capped at 2.0)

### Result Submission

| Mode | Conditions | Endpoint |
|------|------------|----------|
| Training with UID | `current_participant_uid` is set | `POST /api/v1/game/submit` |
| Training without UID | `current_participant_uid` is empty | Local JSON file |
| Competition duel | After game, submits locally then sends score | `POST /api/rooms/{code}/result` |
| Tournament cup | After game, sends match_finished event | `POST /api/tournaments/event` |

All API calls are fire-and-forget with 3 retries + local JSON backup fallback.

### SFX System

Sound effects are generated procedurally using numpy + sounddevice (no .wav files needed):

| Effect | When |
|--------|------|
| `amazing` | Level completed in <10s |
| `great` | Level completed in <15s |
| `solid` | Level completed in <20s |
| `good` | Level completed in <25s |
| `keep_going` | Level completed in <30s |
| `dont_give_up` | Level completed in >30s |
| `next_level` | Advancing to next level |
| `complete` | Test finished |
| `countdown` | 3-2-1 countdown |
| `skip` | Level surrendered (Enter key) |

## Testing

```bash
python -m pytest test_payload.py -v
```

Payload validation tests verify JSON schemas sent to the backend without importing `main.py`. Covers: game result payload, game event types, tournament event schema, room code constraints, participant lookup response.

GUI cannot be unit-tested directly because of the module-level initialization.

## Directory Guide

| Path | Purpose |
|------|---------|
| `main.py` | Entire application (~4900 lines) |
| `MODEL/yolov5/` | Vendored YOLOv5 repo (not application code) |
| `MODEL/exp7/weights/best.pt` | Default hand detection model |
| `MODEL/bantal/bantal.pt` | Alternative pillow detection model |
| `FILES/` | Static test images, loaded by filename at module level |
| `results/` | Local JSON backup directory (created at runtime) |
| `test_payload.py` | JSON payload validation tests |
| `.env.example` | All supported environment variables |
| `requirements.txt` | Python dependencies |

## Important Notes

- **`requests`** is imported inside functions, not at module level. App works offline without it.
- **MediaPipe** import wrapped in try/except; degrades gracefully if unavailable.
- **Global mutable state** (`nick_name`, `gender_code`, `current_participant_uid`, etc.) shared across classes via `global` statements.
- **Camera settings** (device, mirror, zoom) configurable in-app via CameraSettingsDialog, persisted to `.env`.
- **Dark/light theme** controlled by `THEME` env var. Requires restart.
- **`FILES/` assets** loaded by hardcoded filenames at module level — renaming breaks the app.
- **`DLL/`** directory is Windows-only Intel MKL DLLs; irrelevant on Linux/Mac.
- **Window close** on all screens properly cleans up YOLO thread, serial thread, WebSocket, and camera resources.

## Related Repositories

- **`oamp-backend/`** — Go/Gin REST API + WebSocket server (this monorepo)
- **`oamp-frontend/`** — React admin dashboard (this monorepo)