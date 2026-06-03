# BDT Desktop App

Cognitive assessment desktop client — block design test with YOLO-based hand detection, MediaPipe hand tracking, ESP32 physical button support, and multiplayer mode via Go API server.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| GUI | CustomTkinter (dark theme) |
| ML | YOLOv5 + MediaPipe (hand detection) |
| Camera | OpenCV |
| Audio | sounddevice + soundfile |
| Serial | pyserial (ESP32 buttons) |
| HTTP | requests (lazy-loaded, fire-and-forget) |

## Prerequisites

- Python 3.9+
- Camera (built-in or USB webcam)
- Optional: ESP32 with buttons (for `BUTTON_MODE`)

## Setup

```bash
# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate      # Linux/Mac
venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env for your setup
```

## Run

```bash
python main.py
```

Window starts maximized with dark theme.

## Configuration (`.env`)

| Var | Values | Default | Effect |
|-----|--------|---------|--------|
| `PC_MODE` | `competition` / `training` | `training` | `competition`: UID required, multiplayer lobby. `training`: skip to game directly |
| `API_SERVER_URL` | URL or empty | (empty) | Empty = offline solo mode, no server calls |
| `MODEL_BANTAL` | `true` / `false` | `false` | `true`: `MODEL/bantal/bantal.pt`. `false`: `MODEL/exp7/weights/best.pt` |
| `NORMAL_PATTERN` | `true` / `false` | `false` | Switches between two `LEVEL_ANSWERS` dicts and `FILES/` subdirectories |
| `BUTTON_MODE` | `true` / `false` | `false` | Enable ESP32 serial button reader |
| `HIDE_CAMERA` | `true` / `false` | `false` | Hide camera feed entirely |
| `YOLO_SKIP_FRAMES` | integer | `2` | Inference every N+1 frames |
| `MEDIAPIPE_SKIP_FRAMES` | integer | `2` | Same for MediaPipe |
| `CUSTOM_LEVEL` | e.g. `1a,2b,3c` | (empty) | Override random level variant |

## Architecture

Single-file app: all logic in `main.py` (~3500 lines). No modules or packages.

### Classes

| Class | Purpose |
|-------|---------|
| `App_Input` | UID verification screen |
| `App_Room` | Multiplayer lobby (competition mode) |
| `TimeIn` | Main game window (camera + YOLO + scoring) |
| `CameraSettingsDialog` | Camera mirror/zoom settings |
| `YOLODetectionThread` | Background YOLO inference daemon |
| `SerialReaderThread` | ESP32 serial button input |

### Startup (critical)

Heavy initialization runs at module level **before** `if __name__`:
1. `torch` + YOLO model loaded
2. All test images read from `FILES/` into memory
3. Audio, OpenCV, MediaPipe initialized
4. `python-dotenv` loads `.env`

This means importing `main.py` does real work — you cannot unit-test classes in isolation.

### Game Flow

1. **Training mode:** skip to `TimeIn` directly (UID optional)
2. **Competition mode:** `App_Input` (UID scan) -> `App_Room` (create/join lobby) -> `TimeIn` (game)
3. **Tournament cup mode:** UID scan -> `GET /api/tournaments/active-match/:uid` -> if match found, receive room code + opponent, **skip `App_Room`**, join directly

## API Endpoints (competition mode)

Called via fire-and-forget background threads:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/api/v1/participants/uid/{uid}` | Verify participant |
| POST | `/api/v1/game/submit` | Submit game results |
| POST | `/api/game/event` | Game events (incl. heartbeat every 15s) |
| GET | `/api/rooms` | List rooms |
| POST | `/api/rooms` | Create room |
| POST | `/api/rooms/{code}/join` | Join room |
| POST | `/api/rooms/{code}/leave` | Leave room |
| POST | `/api/rooms/{code}/ready` | Set ready |
| GET | `/api/rooms/{code}` | Poll room status |
| GET | `/api/tournaments/active-match/{uid}` | Check active cup match |
| POST | `/api/tournaments/event` | match_started / match_finished |

## Testing

```bash
python -m pytest test_payload.py -v
```

Payload validation tests verify JSON schemas sent to the backend without importing `main.py`. Covers: game result payload, game event types, tournament event schema, room code constraints, participant lookup response.

GUI cannot be unit-tested directly because of the module-level initialization.

## Directory Guide

| Path | Purpose |
|------|---------|
| `main.py` | Entire application |
| `MODEL/yolov5/` | Vendored YOLOv5 repo (not app code) |
| `MODEL/exp7/weights/best.pt` | Default hand detection model |
| `MODEL/bantal/bantal.pt` | Alternative pillow detection model |
| `FILES/` | Static test images, loaded by filename |
| `AUDIO/` | `.wav` clips for audio feedback |
| `DLL/` | Windows-only Intel MKL DLLs |
| `test_payload.py` | JSON payload validation tests |

## Notes

- **`requests`** is imported inside functions, not at module level. App works offline without it.
- **MediaPipe** import wrapped in try/except; degrades gracefully if unavailable.
- **Global mutable state** (`nick_name`, `gender_code`, etc.) shared across classes.
- **Dark theme:** `"Dark"` appearance, `"dark-blue"` color theme. Window starts maximized (`state='zoomed'`).
- **FILES/ assets** loaded by filename at module level — renaming breaks the app.
- **DLL/** directory is Windows-only; irrelevant on Linux/Mac.
