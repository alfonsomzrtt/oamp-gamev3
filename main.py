#>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> Import

import numpy as np
import cv2                                                                        
import os
import math
import time
import sys
import threading
import traceback
from queue import Queue
from dotenv import load_dotenv
from pathlib import Path
import serial
import serial.tools.list_ports
from datetime import datetime

 # Load environment variables from .env file
env_path = Path('.') / '.env'
load_dotenv(dotenv_path=env_path)

ERROR_LOG_PATH = Path(__file__).resolve().parent / "runtime_errors.log"

def log_exception(context, exc_type, exc_value, exc_tb):
    """Log exceptions safely to console and file, bypassing fragile sys.excepthook output."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"[{timestamp}] {context}\n"
    trace = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    payload = f"{header}{trace}\n"

    try:
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(payload)
    except Exception:
        pass

    try:
        sys.__stderr__.write(payload)
        sys.__stderr__.flush()
    except Exception:
        try:
            sys.__stderr__.buffer.write(payload.encode("utf-8", "backslashreplace"))
            sys.__stderr__.buffer.flush()
        except Exception:
            pass

def _global_excepthook(exc_type, exc_value, exc_tb):
    log_exception("Unhandled exception (main thread)", exc_type, exc_value, exc_tb)

def _thread_excepthook(args):
    thread_name = args.thread.name if args.thread else "unknown"
    log_exception(
        f"Unhandled exception (thread: {thread_name})",
        args.exc_type,
        args.exc_value,
        args.exc_traceback,
    )

sys.excepthook = _global_excepthook
if hasattr(threading, "excepthook"):
    threading.excepthook = _thread_excepthook
        
# Get display mode from environment variable (default to True if not set or invalid)
DISPLAY_HALF = os.getenv('DISPLAY_HALF', 'true').lower() == 'true'
NORMAL_PATTERN = os.getenv('NORMAL_PATTERN', 'true').lower() == 'true'
BUTTON_MODE = os.getenv('BUTTON_MODE', 'false').lower() == 'true'
HIDE_CAMERA = os.getenv('HIDE_CAMERA', 'false').lower() == 'true'

# Camera settings
CAMERA_MIRROR_X = os.getenv('CAMERA_MIRROR_X', 'true').lower() == 'true'
CAMERA_MIRROR_Y = os.getenv('CAMERA_MIRROR_Y', 'false').lower() == 'true'
try:
    CAMERA_ZOOM = float(os.getenv('CAMERA_ZOOM', '1.0'))
    if CAMERA_ZOOM < 0.5 or CAMERA_ZOOM > 3.0:
        CAMERA_ZOOM = 1.0
except (ValueError, TypeError):
    CAMERA_ZOOM = 1.0

# PC mode: "competition" = duel / tournament mode selection after UID scan
#          "training" = solo (TimeIn directly), UID optional
PC_MODE   = os.getenv('PC_MODE', 'competition').lower()

# Get max level from environment variable (default to 8 if not set or invalid)
try:
    MAX_LEVEL = int(os.getenv('MAX_LEVEL', '8'))
    if MAX_LEVEL < 1 or MAX_LEVEL > 8:
        MAX_LEVEL = 8
        print(f"Warning: MAX_LEVEL must be between 1 and 8. Using default value: {MAX_LEVEL}")
except (ValueError, TypeError):
    MAX_LEVEL = 8
    print(f"Warning: Invalid MAX_LEVEL in .env file. Using default value: {MAX_LEVEL}")

# ── API Server Config ─────────────────────────────────────────────────────────
API_SERVER_URL    = os.getenv('API_SERVER_URL', '').rstrip('/')
ROOM_ID           = os.getenv('ROOM_ID', 'room_01')
PLAYER_NUM        = int(os.getenv('PLAYER_NUM', '1'))
IS_MULTIPLAYER    = False   # set True when user picks multiplayer mode
CURRENT_ROOM_CODE = ''      # set dynamically when player creates/joins a room

# Tournament cup globals (set after UID scan if active match found)
TOURNAMENT_MODE    = False
TOURNAMENT_ROOM_CODE = ''
TOURNAMENT_OPPONENT = ''
TOURNAMENT_IS_P1   = False
TOURNAMENT_ROUND   = 0

# Base directory for resolving asset paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────────────────────────────────────
#  API Helpers
# ─────────────────────────────────────────────────────────────────────────────

import json as _json

_RESULTS_DIR = os.path.join(BASE_DIR, 'results')
os.makedirs(_RESULTS_DIR, exist_ok=True)
_MAX_RETRIES = 3
_RETRY_BACKOFF = [1, 2, 4]  # seconds between retries


def _api_post(url: str, payload: dict, timeout: int = 10) -> bool:
    """POST with retry. Returns True if success, False after all retries exhausted."""
    for attempt in range(_MAX_RETRIES):
        try:
            import requests as _r
            resp = _r.post(url, json=payload, timeout=timeout)
            if 200 <= resp.status_code < 300:
                return True
            print(f">>> [API] POST {url} returned {resp.status_code} (attempt {attempt+1}/{_MAX_RETRIES})")
        except Exception as e:
            print(f">>> [API] POST {url} error: {e} (attempt {attempt+1}/{_MAX_RETRIES})")
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_BACKOFF[attempt])
    return False


def _save_local_backup(filename: str, payload: dict):
    """Save payload to results/ directory as JSON fallback."""
    try:
        path = os.path.join(_RESULTS_DIR, filename)
        with open(path, 'w') as f:
            _json.dump(payload, f, indent=2, default=str)
        print(f">>> [LOCAL] Backup saved: {path}")
    except Exception as e:
        print(f">>> [LOCAL] Backup write failed: {e}")


def _fire_event(payload: dict):
    """POST /api/game/event with retry in background thread."""
    if not API_SERVER_URL:
        return
    def _do():
        _api_post(f"{API_SERVER_URL}/api/game/event", payload, timeout=5)
    threading.Thread(target=_do, daemon=True).start()


def _verify_participant(uid: str, callback=None):
    """GET /api/v1/participants/uid/{uid} — verify and return participant data."""
    if not API_SERVER_URL:
        if callback:
            callback(False, {})
        return
    def _do():
        try:
            import requests as _r
            resp = _r.get(f"{API_SERVER_URL}/api/v1/participants/uid/{uid}", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                callback(True, data)
            else:
                callback(False, {})
        except Exception:
            callback(False, {})
    threading.Thread(target=_do, daemon=True).start()


def _submit_results(uid: str, payload: dict):
    """POST /api/v1/game/submit with retry + local backup fallback."""
    if not API_SERVER_URL:
        _save_local_backup(f"offline_{uid}_{int(time.time())}.json", payload)
        return
    def _do():
        ok = _api_post(f"{API_SERVER_URL}/api/v1/game/submit", payload, timeout=10)
        if ok:
            print(f">>> [API] Results submitted for UID: {uid}")
        else:
            print(f">>> [API] Submit FAILED after {_MAX_RETRIES} retries — saving locally")
            _save_local_backup(f"failed_{uid}_{int(time.time())}.json", payload)
    threading.Thread(target=_do, daemon=True).start()


def _save_training_locally(payload: dict):
    """Save anonymous training results for dataset collection."""
    _save_local_backup(f"training_{int(time.time())}.json", payload)


def _check_tournament_match(uid: str, callback=None):
    """GET /api/tournaments/active-match/{uid} — check if UID has an active cup match."""
    if not API_SERVER_URL:
        if callback:
            callback(False, {})
        return
    def _do():
        try:
            import requests as _r
            resp = _r.get(f"{API_SERVER_URL}/api/tournaments/active-match/{uid}", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success" and data["data"].get("has_match"):
                    callback(True, data["data"])
                else:
                    callback(False, {})
            else:
                callback(False, {})
        except Exception:
            callback(False, {})
    threading.Thread(target=_do, daemon=True).start()


def _send_tournament_event(room_code: str, event_type: str, player_num: int = 0, score: float = 0):
    """POST /api/tournaments/event — match_started / match_finished."""
    if not API_SERVER_URL:
        return
    def _do():
        try:
            import requests as _r
            payload = {
                "room_id":     room_code,
                "event_type":  event_type,
            }
            if player_num > 0:
                payload["player_num"] = player_num
            if score > 0:
                payload["score"] = score
            resp = _r.post(f"{API_SERVER_URL}/api/tournaments/event", json=payload, timeout=5)
            print(f">>> [TOURNAMENT] Event '{event_type}' sent — status {resp.status_code}")
        except Exception as e:
            print(f">>> [TOURNAMENT] Event '{event_type}' failed: {e}")
    threading.Thread(target=_do, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────

#>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> Learning

import torch
 
# Device configuration
print("GPU CUDA is Available : " + str(torch.cuda.is_available()))
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

USE_BANTAL_MODEL = os.getenv('MODEL_BANTAL', 'false').lower() == 'true'
if USE_BANTAL_MODEL:
    PATH_MODEL = os.path.join(BASE_DIR, 'MODEL', 'bantal', 'bantal.pt')
    
    try:
        print(">>> Loading bantal.pt with Ultralytics format...")
        
        # Cek apakah ini model Ultralytics (v8+ atau v7.0+)
        checkpoint = torch.load(PATH_MODEL, map_location='cpu', weights_only=False)
        
        if 'version' in checkpoint:
            print(f">>> Model version: {checkpoint['version']}")
        
        # Jika model dari Ultralytics (punya key 'version')
        if 'version' in checkpoint and hasattr(checkpoint['model'], '__class__') and \
           'ultralytics' in str(checkpoint['model'].__class__):
            
            print(">>> Detected Ultralytics format, loading with ultralytics library...")
            
            # Coba load dengan ultralytics library
            try:
                from ultralytics import YOLO
                model_yolo = YOLO(PATH_MODEL)
                model_yolo.to(device)
                print(">>> Model bantal.pt loaded successfully with Ultralytics YOLO")
            except ImportError:
                print(">>> Ultralytics library not found, installing...")
                import subprocess
                subprocess.check_call([sys.executable, "-m", "pip", "install", "ultralytics"])
                from ultralytics import YOLO
                model_yolo = YOLO(PATH_MODEL)
                model_yolo.to(device)
                print(">>> Model bantal.pt loaded successfully with Ultralytics YOLO")
        
        else:
            # Fallback: gunakan attempt_load biasa
            print(">>> Loading with attempt_load...")
            import sys
            sys.path.insert(0, os.path.join(BASE_DIR, 'MODEL', 'yolov5'))
            from models.experimental import attempt_load
            
            model_yolo = attempt_load(PATH_MODEL, device=device)
            
            # Inisialisasi grid untuk model lama
            for m in model_yolo.modules():
                if type(m).__name__ == 'Detect':
                    if not hasattr(m, 'grid'):
                        m.grid = [torch.zeros(1)] * m.nl
                    if not hasattr(m, 'anchor_grid'):
                        m.anchor_grid = [torch.zeros(1)] * m.nl
            
            model_yolo.to(device)
            print(">>> Model bantal.pt loaded successfully with attempt_load")
    
    except Exception as e:
        print(f">>> Failed to load bantal.pt: {e}")
        print(">>> Falling back to best.pt...")
        USE_BANTAL_MODEL = False
        PATH_MODEL = os.path.join(BASE_DIR, 'MODEL', 'exp7', 'weights', 'best.pt')

if not USE_BANTAL_MODEL:
    PATH_MODEL = os.path.join(BASE_DIR, 'MODEL', 'exp7', 'weights', 'best.pt')
    model_yolo = torch.hub.load(
        os.path.join(BASE_DIR, 'MODEL', 'yolov5'), 
        'custom', 
        path=PATH_MODEL, 
        force_reload=True, 
        source='local'
    )
    model_yolo.to(device)


#>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> Read Files

task_00 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_1000x1000', '00.jpg'))
task_0A = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_1000x1000', '0A.jpg'))
task_0B = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_1000x1000', '0Bx.jpg'))
task_09 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_1000x1000', '09.jpg'))

# Random Level
import random
LEVEL_VARIANTS = {
    1: ['1a', '1b', '1c', '1d'],
    2: ['2a', '2b', '2c', '2d'],
    3: ['3a', '3b', '3c', '3d'],
    4: ['4a', '4b', '4c', '4d'],
    5: ['5a', '5b', '5c', '5d'],
    6: ['6a', '6b', '6c', '6d'],
    7: ['7a', '7b', '7c', '7d'],
    8: ['8a', '8b', '8c', '8d']
}
def parse_custom_levels(custom_level_str):
    """Parse and validate custom levels from string format.
    
    Args:
        custom_level_str: String in format '1a,2b,3c,4d,5d,6c,7b,8a' or empty string
        
    Returns:
        dict: Dictionary mapping level numbers to their custom variants, or empty dict if invalid
    """
    if not custom_level_str or custom_level_str == '[]':
        return {}
        
    try:
        # Remove any whitespace and split by comma
        levels = [lvl.strip() for lvl in custom_level_str.split(',') if lvl.strip()]
        
        # Validate each level
        custom_levels = {}
        for level in levels:
            if len(level) != 2:
                print(f"Invalid level format: {level}. Must be like '1a', '2b', etc.")
                return {}
                
            level_num = level[0]
            variant = level[1].lower()
            
            # Validate level number (1-8)
            if not level_num.isdigit() or int(level_num) < 1 or int(level_num) > 8:
                print(f"Invalid level number in {level}. Must be 1-8.")
                return {}
                
            # Validate variant (a-d)
            if variant not in ['a', 'b', 'c', 'd']:
                print(f"Invalid variant in {level}. Must be a, b, c, or d.")
                return {}
                
            custom_levels[int(level_num)] = f"{level_num}{variant}"
            
        return custom_levels
        
    except Exception as e:
        print(f"Error parsing CUSTOM_LEVEL: {e}")
        return {}

def sanitize_env_value(raw_value):
    """Trim env values and remove inline comments (e.g., '1a,2b # notes')."""
    if raw_value is None:
        return ''
    value = str(raw_value).strip()
    if '#' in value:
        value = value.split('#', 1)[0].strip()
    return value

# Parse and validate custom levels
CUSTOM_LEVEL_STR = sanitize_env_value(os.getenv('CUSTOM_LEVEL', ''))
CUSTOM_LEVELS = parse_custom_levels(CUSTOM_LEVEL_STR)

# If we have valid custom levels, print them
if CUSTOM_LEVELS:
    print(f"Using custom levels: {CUSTOM_LEVELS}")
else:
    print("No valid custom levels specified, using random variants")
LEVEL_PATHS = {}
for variants in LEVEL_VARIANTS.values():
    for variant in variants:
        level_num = variant[0]  # Get the level number (1-8)
        if NORMAL_PATTERN:
            LEVEL_PATHS[variant] = os.path.join(
                BASE_DIR, 
                'FILES', 
                'TEST_RANDOM_1500x1500', 
                f'Lvl {level_num}{variant[1]}.png'  # e.g., 'Lvl 1a.png'
            )
        else:
            LEVEL_PATHS[variant] = os.path.join(
                BASE_DIR, 
                'FILES', 
                'TEST_RANDOM_OTAK_ATIK', 
                f'Lvl {level_num}{variant[1]}.PNG'  # e.g., 'Lvl 1a.png'
            )

#scale_percent = 70 # percent of original size
#task_width = int(task_0B.shape[1] * scale_percent / 100)
#task_height = int(task_0B.shape[0] * scale_percent / 100)
#task_dim = (task_width, task_height)

#task_resized = cv2.resize(task_0B, task_dim, interpolation = cv2.INTER_AREA)
#cv2.imshow('Task', task_0B)


face_01 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'LABEL_50x50', '01.png'), cv2.IMREAD_COLOR)
face_02 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'LABEL_50x50', '02.png'), cv2.IMREAD_COLOR)
face_03 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'LABEL_50x50', '03.png'), cv2.IMREAD_COLOR)
face_04 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'LABEL_50x50', '04.png'), cv2.IMREAD_COLOR)
face_05 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'LABEL_50x50', '05.png'), cv2.IMREAD_COLOR)
face_06 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'LABEL_50x50', '06.png'), cv2.IMREAD_COLOR)

gray_face_01 = cv2.cvtColor(face_01, cv2.COLOR_BGR2GRAY)
gray_face_02 = cv2.cvtColor(face_02, cv2.COLOR_BGR2GRAY)
gray_face_03 = cv2.cvtColor(face_03, cv2.COLOR_BGR2GRAY)
gray_face_04 = cv2.cvtColor(face_04, cv2.COLOR_BGR2GRAY)
gray_face_05 = cv2.cvtColor(face_05, cv2.COLOR_BGR2GRAY)
gray_face_06 = cv2.cvtColor(face_06, cv2.COLOR_BGR2GRAY)

ret_01, mask_face_01 = cv2.threshold(gray_face_01, 1, 255, cv2.THRESH_BINARY)
ret_02, mask_face_02 = cv2.threshold(gray_face_02, 1, 255, cv2.THRESH_BINARY)
ret_03, mask_face_03 = cv2.threshold(gray_face_03, 1, 255, cv2.THRESH_BINARY)
ret_04, mask_face_04 = cv2.threshold(gray_face_04, 1, 255, cv2.THRESH_BINARY)
ret_05, mask_face_05 = cv2.threshold(gray_face_05, 1, 255, cv2.THRESH_BINARY)
ret_06, mask_face_06 = cv2.threshold(gray_face_06, 1, 255, cv2.THRESH_BINARY)

img_00 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_100x100', '00.jpg'))
img_01 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_100x100', '01.jpg'))
img_02 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_100x100', '02.jpg'))
img_03 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_100x100', '03.jpg'))
img_04 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_100x100', '04.jpg'))
img_05 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_100x100', '05.jpg'))
img_06 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_100x100', '06.jpg'))
img_07 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_100x100', '07.jpg'))
img_08 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_100x100', '08.jpg'))
img_09 = cv2.imread(os.path.join(BASE_DIR, 'FILES', 'TEST_100x100', '09.jpg'))

gray_img_00 = cv2.cvtColor(img_00, cv2.COLOR_BGR2GRAY)
gray_img_01 = cv2.cvtColor(img_01, cv2.COLOR_BGR2GRAY)
gray_img_02 = cv2.cvtColor(img_02, cv2.COLOR_BGR2GRAY)
gray_img_03 = cv2.cvtColor(img_03, cv2.COLOR_BGR2GRAY)
gray_img_04 = cv2.cvtColor(img_04, cv2.COLOR_BGR2GRAY)
gray_img_05 = cv2.cvtColor(img_05, cv2.COLOR_BGR2GRAY)
gray_img_06 = cv2.cvtColor(img_06, cv2.COLOR_BGR2GRAY)
gray_img_07 = cv2.cvtColor(img_07, cv2.COLOR_BGR2GRAY)
gray_img_08 = cv2.cvtColor(img_08, cv2.COLOR_BGR2GRAY)
gray_img_09 = cv2.cvtColor(img_09, cv2.COLOR_BGR2GRAY)

ret_img_00, mask_img_00 = cv2.threshold(gray_img_00, 1, 255, cv2.THRESH_BINARY)
ret_img_01, mask_img_01 = cv2.threshold(gray_img_01, 1, 255, cv2.THRESH_BINARY)
ret_img_02, mask_img_02 = cv2.threshold(gray_img_02, 1, 255, cv2.THRESH_BINARY)
ret_img_03, mask_img_03 = cv2.threshold(gray_img_03, 1, 255, cv2.THRESH_BINARY)
ret_img_04, mask_img_04 = cv2.threshold(gray_img_04, 1, 255, cv2.THRESH_BINARY)
ret_img_05, mask_img_05 = cv2.threshold(gray_img_05, 1, 255, cv2.THRESH_BINARY)
ret_img_06, mask_img_06 = cv2.threshold(gray_img_06, 1, 255, cv2.THRESH_BINARY)
ret_img_07, mask_img_07 = cv2.threshold(gray_img_07, 1, 255, cv2.THRESH_BINARY)
ret_img_08, mask_img_08 = cv2.threshold(gray_img_08, 1, 255, cv2.THRESH_BINARY)
ret_img_09, mask_img_09 = cv2.threshold(gray_img_09, 1, 255, cv2.THRESH_BINARY)


#>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> Global Parameter

n_frame = 0    
n_capture = 3            # Detail 1 Normal 3  # Realtime 7
#n_contour = 0
n_test = 0

timer_return = 0

t_thumb_all = []

t_thumb_01 = 0
t_thumb_02 = 0
t_thumb_03 = 0
t_thumb_03 = 0
t_thumb_04 = 0
t_thumb_05 = 0
t_thumb_06 = 0
t_thumb_07 = 0
t_thumb_08 = 0

thumb_flag_01 = True
thumb_flag_02 = True
thumb_flag_03 = True
thumb_flag_04 = True
thumb_flag_05 = True
thumb_flag_06 = True
thumb_flag_07 = True
thumb_flag_08 = True

# Answers for static question
# ans_01  = [2,1,1,2]
# ans_02  = [1,3,1,1]
# ans_02x = [1,1,3,1]
# ans_03  = [2,2,3,4]
# ans_03x = [2,3,2,4]
# ans_04  = [5,1,4,1]
# ans_04x = [5,4,1,1]
# ans_05  = [4,3,5,6]
# ans_05x = [4,5,3,6]
# ans_06  = [1,4,6,1]
# ans_06x = [1,6,4,1]
# ans_07  = [5,6,4,3]
# ans_07x = [5,4,6,3]
# ans_08  = [5,3,4,5]
# ans_08x = [5,4,3,5]

# Answers for random question
if NORMAL_PATTERN:
    LEVEL_ANSWERS = {
        '1a': [2,1,1,1], '1b': [2,1,1,2], '1c': [1,1,1,2], '1d': [1,2,2,1],
        '2a': [2,1,6,1], '2b': [3,2,1,2], '2c': [2,2,5,6], '2d': [6,2,2,6],
        '3a': [3,5,1,1], '3b': [1,5,1,6], '3c': [2,3,4,1], '3d': [6,5,3,4],
        '4a': [6,2,1,6], '4b': [3,2,2,5], '4c': [1,5,3,1], '4d': [4,2,2,6],
        '5a': [6,3,5,4], '5b': [5,4,6,3], '5c': [3,5,6,4], '5d': [4,4,6,6],
        '6a': [1,4,3,1], '6b': [6,1,5,5], '6c': [5,1,1,4], '6d': [5,4,4,5],
        '7a': [4,5,5,5], '7b': [4,5,5,6], '7c': [2,5,1,5], '7d': [1,2,3,6],
        '8a': [6,5,6,3], '8b': [6,3,4,2], '8c': [5,6,3,5], '8d': [3,6,5,4],
    } 
else:
    LEVEL_ANSWERS = {
        '1a': [1,2,1,2], '1b': [2,1,2,1], '1c': [2,1,1,2], '1d': [1,2,2,1],
        '2a': [2,4,1,1], '2b': [1,6,2,2], '2c': [1,2,2,4], '2d': [2,1,1,6],
        '3a': [4,1,5,1], '3b': [6,2,3,2], '3c': [5,1,4,1], '3d': [3,2,6,2],
        '4a': [4,1,1,6], '4b': [6,2,2,4], '4c': [1,5,3,1], '4d': [2,3,5,2],
        '5a': [4,3,5,6], '5b': [6,5,3,4], '5c': [6,6,6,6], '5d': [4,4,4,4],
        '6a': [5,3,4,6], '6b': [3,5,6,4], '6c': [6,6,4,4], '6d': [4,4,6,6],
        '7a': [3,4,6,5], '7b': [5,6,4,3], '7c': [3,6,4,5], '7d': [5,4,6,3],
        '8a': [5,4,4,5], '8b': [3,6,6,3], '8c': [4,5,3,6], '8d': [6,3,5,4],
    } 

grasp_pose = [0,0,0,0,0,0,0]

speed_eye = 0
speed_hand = 0

scale_percent = 130 # percent of original size
task_width = int(task_0A.shape[1] * scale_percent / 100)
task_height = int(task_0A.shape[0] * scale_percent / 100)
task_dim = (task_width, task_height)
task_resized = cv2.resize(task_0A, task_dim, interpolation = cv2.INTER_AREA)

nick_name = ""
gender_code = ""
age_range_code = 0
current_participant_uid = ""



#>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> Text to  

# from g2p_id import G2P
# from TTS.api import TTS  # dari library Coqui TTS
import sounddevice as sd
import soundfile as sf

def play_audio(wav):
    """
    wav: either a filepath (str / Path) or a numpy array of samples.
    If filepath -> read file with soundfile so we get data and samplerate.
    """
    # jika path diberikan, baca file
    if isinstance(wav, (str, Path)):
        path = str(wav)
        # cek ada file
        if not Path(path).exists():
            raise FileNotFoundError(f"Audio file not found: {path}")
        data, samplerate = sf.read(path, dtype='float32')  # data float32 in [-1,1]
    else:
        # asumsi sudah array-like
        data = np.asarray(wav)
        samplerate = 22050  # default jika pemanggil memang mengirim data langsung

    # pastikan bentuk (frames, channels)
    if data.ndim == 1:
        data = data.reshape(-1, 1)

    sd.play(data, samplerate)
    sd.wait()

try:
    import mediapipe as mp
except Exception as e:
    mp = None
    print(f">>> MediaPipe import failed: {e}. Hand landmark detection disabled.")

mp_drawing = None
mp_hands = None
landmark_style = None
connection_style = None

if mp is not None:
    try:
        mp_solutions = getattr(mp, "solutions", None)
        if mp_solutions is None:
            from mediapipe.python import solutions as mp_solutions
        mp_drawing = mp_solutions.drawing_utils
        mp_hands = mp_solutions.hands
        landmark_style = mp_drawing.DrawingSpec(color=(0,0,255), thickness=3, circle_radius=3)
        connection_style = mp_drawing.DrawingSpec(color=(0,0,255), thickness=4)
    except Exception as e:
        print(f">>> MediaPipe 'solutions' API unavailable: {e}. Hand landmark detection disabled.")



#>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> Setup GUI Input 

import customtkinter
from PIL import Image, ImageTk

customtkinter.set_appearance_mode("dark")
customtkinter.set_default_color_theme("dark-blue")

# ── Theme System ──────────────────────────────────────────────────────────────
_THEME = os.getenv('THEME', 'dark').lower()

def _theme_color(dark_val, light_val):
    return dark_val if _THEME == 'dark' else light_val

# Refined modern palette — high contrast, accessible, less aggressive than pure red
_CLR_BG            = _theme_color("#0F0F0F", "#FFFFFF")
_CLR_CARD          = _theme_color("#1A1A1A", "#F8F9FA")
_CLR_ACCENT        = "#E63946"   # deep crimson — energetic but refined
_CLR_ACCENT2       = "#C1121F"   # darker crimson hover
_CLR_TEXT          = _theme_color("#F1F5F9", "#0F172A")
_CLR_MUTED         = _theme_color("#94A3B8", "#64748B")
_CLR_BORDER        = "#E63946"   # accent border
_CLR_SUBTLE_BORDER = _theme_color("#2D2D2D", "#E9ECEF")
_CLR_TIMER_BG      = _theme_color("#1A1A1A", "#F8F9FA")
_CLR_DANGER        = "#E63946"
_CLR_DANGER_BG     = _theme_color("#1A0505", "#FFF0F0")
_CLR_SUCCESS       = "#2A9D8F"   # teal
_CLR_SUCCESS_BG    = _theme_color("#0A1F1C", "#E6F7F5")
_CLR_SUCCESS_HOVER = _theme_color("#236C63", "#B8E0D9")
_CLR_DANGER_HOVER  = "#9B2226"
_CLR_WARNING       = "#F4A261"   # warm amber
_CLR_TEAL          = _theme_color("#94A3B8", "#64748B")

# Corner radii — modern but not bubbly
_CORNER_RADIUS       = 8
_CORNER_RADIUS_SMALL = 4
_CORNER_RADIUS_PILL  = 12

def _set_theme(new_theme: str):
    """Persist theme to .env and return whether restart is needed."""
    global _THEME
    if new_theme not in ('dark', 'light'):
        return False
    _THEME = new_theme
    env_path = Path('.') / '.env'
    lines = []
    if env_path.exists():
        with open(env_path, 'r') as f:
            lines = f.readlines()
    updated = False
    new_lines = []
    for line in lines:
        if line.strip().startswith('THEME='):
            new_lines.append(f'THEME={new_theme}\n')
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f'THEME={new_theme}\n')
    with open(env_path, 'w') as f:
        f.writelines(new_lines)
    return True

# ── Typography ────────────────────────────────────────────────────────────────
# Modern system font stack — clean, crisp, good cross-platform coverage
_FONT_PRIMARY   = ("Segoe UI", "Roboto", "Helvetica Neue", "Arial", "sans-serif")
_FONT_MONO      = ("SF Mono", "Fira Code", "Consolas", "Courier New", "monospace")
# ─────────────────────────────────────────────────────────────────────────────

# Input Data
nick_name       = "NULL"
gender_code     = "NULL"
age_range_code  = "NULL"

class MyTextboxFrame(customtkinter.CTkFrame):
    def __init__(self, master, title, values):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)

        self.textbox = customtkinter.CTkEntry(
            master=self, width=200, height=52,
            corner_radius=_CORNER_RADIUS, font=(_FONT_PRIMARY[0], 26), justify="center",
            placeholder_text="Ketik atau scan UID...",
            border_width=2, border_color=_CLR_SUBTLE_BORDER,
            fg_color=_CLR_BG,
            text_color=_CLR_TEXT,
        )
        self.textbox.grid(row=0, column=0, sticky="ew")

    def get(self):
        return self.textbox.get().strip()

    def set_text(self, text: str):
        self.textbox.delete(0, "end")
        self.textbox.insert(0, text)

    def bind_return(self, callback):
        self.textbox.bind("<Return>", lambda e: callback())


class App_Input(customtkinter.CTk): #CTkToplevel
    def __init__(self):
        super().__init__()

        self.title("Block Design Test")
        self.geometry("520x680")
        self.configure(fg_color=_CLR_BG)
        self.resizable(0, 0)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.grid_columnconfigure(0, weight=1)

        # ── Title Block ──────────────────────────────────────────────────────
        customtkinter.CTkLabel(
            self,
            text="BLOCK DESIGN TEST",
            font=(_FONT_PRIMARY[0], 34, "bold"),
            text_color=_CLR_TEXT,
        ).grid(row=0, column=0, pady=(40, 0), sticky="n")
        customtkinter.CTkLabel(
            self,
            text="Otak Atik Tournament",
            font=(_FONT_PRIMARY[0], 14),
            text_color=_CLR_ACCENT,
        ).grid(row=1, column=0, pady=(6, 0), sticky="n")

        # Theme toggle (top-right corner)
        theme_label = "Light" if _THEME == "dark" else "Dark"
        self.theme_btn = customtkinter.CTkButton(
            self, text=theme_label,
            command=self._toggle_theme,
            font=(_FONT_PRIMARY[0], 11, "bold"),
            fg_color=_CLR_CARD, hover_color=_CLR_SUBTLE_BORDER,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS_PILL, height=30, width=70,
        )
        self.theme_btn.place(relx=0.96, rely=0.03, anchor="ne")

        # Refined accent line — full width
        line = customtkinter.CTkFrame(self, fg_color=_CLR_ACCENT, height=3, corner_radius=0)
        line.grid(row=2, column=0, sticky="ew", padx=0, pady=(24, 32))

        # ── UID Section ──────────────────────────────────────────────────────
        customtkinter.CTkLabel(
            self, text="NOMOR PESERTA",
            font=(_FONT_PRIMARY[0], 12, "bold"),
            text_color=_CLR_MUTED,
        ).grid(row=3, column=0, sticky="w", padx=28, pady=(0, 8))

        self.textbox_frame_uid = MyTextboxFrame(self, "UID", values=" ")
        self.textbox_frame_uid.grid(row=4, column=0, sticky="ew", padx=28, pady=(0, 12))
        self.textbox_frame_uid.bind_return(self._cek_uid)

        self.cek_uid_btn = customtkinter.CTkButton(
            self, text="CEK",
            command=self._cek_uid,
            font=(_FONT_PRIMARY[0], 15, "bold"),
            fg_color=_CLR_ACCENT, hover_color=_CLR_ACCENT2,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS, height=52,
        )
        self.cek_uid_btn.grid(row=5, column=0, sticky="ew", padx=28, pady=(0, 20))

        # ── Participant Info Box ─────────────────────────────────────────────
        self.info_box = customtkinter.CTkFrame(
            self, fg_color=_CLR_CARD, corner_radius=_CORNER_RADIUS,
            border_width=1, border_color=_CLR_SUBTLE_BORDER,
        )
        self.info_box.grid(row=6, column=0, sticky="ew", padx=28, pady=(0, 0))
        self.info_box.grid_columnconfigure(0, weight=1)
        self.info_box.grid_remove()  # Hidden until UID verified

        self.participant_info = customtkinter.CTkLabel(
            self.info_box,
            text="Belum ada peserta",
            font=(_FONT_PRIMARY[0], 14),
            text_color=_CLR_MUTED,
        )
        self.participant_info.grid(row=0, column=0, padx=20, pady=(16, 6), sticky="w")

        self.reset_btn = customtkinter.CTkButton(
            self.info_box, text="Ganti",
            command=self._reset_scan,
            font=(_FONT_PRIMARY[0], 11),
            fg_color="transparent", hover_color=_CLR_SUBTLE_BORDER,
            text_color=_CLR_MUTED, height=26, width=64,
            corner_radius=_CORNER_RADIUS_SMALL,
        )
        self.reset_btn.grid(row=1, column=0, padx=20, pady=(0, 14), sticky="w")

        # Status text below info box
        self.uid_status = customtkinter.CTkLabel(
            self, text="",
            font=(_FONT_PRIMARY[0], 13), text_color=_CLR_ACCENT,
        )
        self.uid_status.grid(row=7, column=0, padx=28, pady=(10, 0), sticky="w")

        # ── Spacer ───────────────────────────────────────────────────────────
        spacer = customtkinter.CTkFrame(self, fg_color="transparent", height=20)
        spacer.grid(row=8, column=0, sticky="ew")

        # ── Main Action Button ───────────────────────────────────────────────
        self.button = customtkinter.CTkButton(
            self,
            text="MULAI TES",
            command=self.button_callback,
            font=(_FONT_PRIMARY[0], 22, "bold"),
            fg_color=_CLR_ACCENT,
            hover_color=_CLR_ACCENT2,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS,
            height=64,
        )
        self.button.grid(row=9, column=0, sticky="ew", padx=28, pady=(0, 12))

        # Competition mode buttons (hidden until UID verified)
        self.mode_frame = customtkinter.CTkFrame(self, fg_color="transparent")
        self.mode_frame.grid(row=9, column=0, sticky="ew", padx=28, pady=(0, 12))
        self.mode_frame.grid_columnconfigure((0, 1), weight=1)
        self.mode_frame.grid_remove()

        self.duel_btn = customtkinter.CTkButton(
            self.mode_frame,
            text="Duel 1v1",
            command=self._start_duel,
            font=(_FONT_PRIMARY[0], 14, "bold"),
            fg_color=_CLR_ACCENT,
            hover_color=_CLR_ACCENT2,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS,
            height=56,
        )
        self.duel_btn.grid(row=0, column=0, padx=(0, 6), sticky="ew")

        self.tournament_btn = customtkinter.CTkButton(
            self.mode_frame,
            text="Turnamen",
            command=self._start_tournament,
            font=(_FONT_PRIMARY[0], 14, "bold"),
            fg_color=_CLR_CARD,
            hover_color=_CLR_ACCENT,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS,
            height=56,
        )
        self.tournament_btn.grid(row=0, column=1, padx=(6, 0), sticky="ew")

        # In training mode
        if PC_MODE == "training":
            self._uid_required = False
            self.uid_status.configure(text="Mode Latihan — UID opsional", text_color=_CLR_MUTED)
            self.button.configure(text="Mulai Latihan")
        else:
            self._uid_required = True

    def _on_close(self):
        raise SystemExit(0)

    def _toggle_theme(self):
        new_theme = 'light' if _THEME == 'dark' else 'dark'
        _set_theme(new_theme)
        import tkinter.messagebox as mb
        mb.showinfo("Tema Diubah", "Restart aplikasi untuk menerapkan tema baru.")
        self.theme_btn.configure(text="Dark" if new_theme == 'light' else "Light")

    def report_callback_exception(self, exc, val, tb):
        log_exception("Tk callback exception (App_Input)", exc, val, tb)

    def _cek_uid(self):
        """Verify UID against server, autofill participant data."""
        uid = self.textbox_frame_uid.get().strip()
        if not uid:
            self.uid_status.configure(text="Masukkan UID dulu", text_color=_CLR_DANGER)
            return
        self.cek_uid_btn.configure(state="disabled", text="...")
        self.reset_btn.configure(state="disabled")
        self.uid_status.configure(text="Mengecek ke server...", text_color=_CLR_MUTED)

        def on_result(exists, data):
            self.after(0, lambda: self._on_uid_result(exists, data, uid))

        _verify_participant(uid, callback=on_result)

    def _reset_scan(self):
        """Clear current participant and reset UI to initial state."""
        global current_participant_uid, nick_name, gender_code, age_range_code, TOURNAMENT_MODE
        current_participant_uid = ""
        nick_name = ""
        gender_code = ""
        age_range_code = 0
        TOURNAMENT_MODE = False

        self.textbox_frame_uid.set_text(" ")
        self.participant_info.configure(text="Belum ada peserta", text_color=_CLR_MUTED)
        self.uid_status.configure(text="", text_color=_CLR_MUTED)
        self.cek_uid_btn.configure(state="normal", text="CEK")
        self.reset_btn.configure(state="normal")
        self.info_box.grid_remove()

        if PC_MODE == "training":
            self.uid_status.configure(text="Mode Latihan — UID opsional", text_color=_CLR_MUTED)
        else:
            self.button.grid()          # show training-style button
            self.mode_frame.grid_remove()  # hide mode selection

    def _on_uid_result(self, exists, data, uid):
        """Handle verify result — store UID + autofill participant data from server."""
        self.cek_uid_btn.configure(state="normal", text="Cek")
        self.reset_btn.configure(state="normal")
        if not exists:
            import tkinter.messagebox as mb
            self.uid_status.configure(text=f"UID '{uid}' tidak ditemukan di server", text_color=_CLR_DANGER)
            mb.showerror("UID Tidak Ditemukan", f"UID '{uid}' tidak ada di server.")
            global TOURNAMENT_MODE
            TOURNAMENT_MODE = False
            return

        # Unwrap response envelope: backend v1 routes return {"status","message","data"}
        participant = data.get("data", {}) if isinstance(data, dict) else {}

        global current_participant_uid, nick_name, gender_code, age_range_code
        current_participant_uid = uid
        nick_name = participant.get("name", "")
        gender_code = participant.get("gender", "")
        age_range_code = int(participant.get("age", 0)) if participant.get("age") else 0

        age_display = participant.get("age", "?")
        gender_display = participant.get("gender", "?")
        self.participant_info.configure(
            text=f"{nick_name}  |  {age_display} th  |  {gender_display}",
            text_color=_CLR_TEXT
        )
        self.info_box.grid()
        print(f">>> UID verified: {uid}, participant: {nick_name}")

        if PC_MODE == "competition":
            self.uid_status.configure(text="Mengecek jadwal turnamen...", text_color=_CLR_MUTED)
            def _on_tournament_check(has_match, match_data):
                self.after(0, lambda: self._on_tournament_result(has_match, match_data))
            _check_tournament_match(uid, callback=_on_tournament_check)
        else:
            self.uid_status.configure(text="Siap mulai", text_color=_CLR_SUCCESS)

    def _on_tournament_result(self, has_match, match_data):
        """Handle tournament active-match response — show Duel / Turnamen buttons."""
        global TOURNAMENT_MODE, TOURNAMENT_ROOM_CODE, TOURNAMENT_OPPONENT, TOURNAMENT_IS_P1, TOURNAMENT_ROUND
        if has_match:
            TOURNAMENT_MODE = True
            TOURNAMENT_ROOM_CODE = match_data.get("room_id", "")
            TOURNAMENT_OPPONENT = match_data.get("opponent", "?")
            TOURNAMENT_IS_P1 = match_data.get("is_player1", False)
            TOURNAMENT_ROUND = match_data.get("match", {}).get("round", 0)

            slot = "P1" if TOURNAMENT_IS_P1 else "P2"
            round_name = {1: "Round 1", 2: "Quarterfinal", 3: "Semifinal", 4: "Final"}.get(TOURNAMENT_ROUND, f"Ronde {TOURNAMENT_ROUND}")

            self.uid_status.configure(
                text=f"{round_name}  |  Lawan: {TOURNAMENT_OPPONENT}  |  Slot: {slot}",
                text_color=_CLR_SUCCESS,
            )
            print(f">>> Tournament match found: {TOURNAMENT_ROOM_CODE} vs {TOURNAMENT_OPPONENT} ({slot})")
        else:
            TOURNAMENT_MODE = False
            self.uid_status.configure(text="Siap mulai", text_color=_CLR_SUCCESS)
            print(">>> No active tournament match for this UID")

        # Show mode selection buttons in competition mode
        if PC_MODE == "competition":
            self.button.grid_remove()
            self.mode_frame.grid()
            self.duel_btn.configure(state="normal")
            if has_match:
                self.tournament_btn.configure(state="normal", fg_color=_CLR_SUCCESS, hover_color=_CLR_SUCCESS_HOVER)
            else:
                self.tournament_btn.configure(state="disabled", fg_color=_CLR_SUBTLE_BORDER, hover_color=_CLR_SUBTLE_BORDER)

    def _start_duel(self):
        """Start duel 1v1 mode — go to App_Room lobby."""
        global TOURNAMENT_MODE
        TOURNAMENT_MODE = False
        print(">>> Mode selected: Duel 1v1")
        self.destroy()

    def _start_tournament(self):
        """Start tournament cup mode — use pre-assigned room code."""
        global TOURNAMENT_MODE
        TOURNAMENT_MODE = True
        print(">>> Mode selected: Turnamen")
        self.destroy()

    def button_callback(self):
        import tkinter.messagebox as messagebox
        try:
            uid = self.textbox_frame_uid.get().strip()

            if not uid:
                if self._uid_required:
                    raise ValueError("UID tidak boleh kosong — tekan 'Cek UID' dulu")
                # Training mode: use placeholder data
                global nick_name, gender_code, age_range_code, current_participant_uid
                nick_name = "training_user"
                gender_code = ""
                age_range_code = 0
                current_participant_uid = ""
                print("Name   :", nick_name)
                print("UID    : (none — training mode)")
            elif not current_participant_uid:
                raise ValueError("UID belum diverifikasi — tekan 'Cek UID' dulu")
            else:
                print("Name   :", nick_name)
                print("Age    :", age_range_code)
                print("Gender :", gender_code)
                print("UID    :", current_participant_uid)

            self.destroy()

        except ValueError as e:
            messagebox.showerror("Input Error", str(e))
        except Exception as e:
            print(f"Unexpected error: {e}")
            messagebox.showerror("Error", "Terjadi kesalahan saat memproses input")


try:
    if PC_MODE == "training":
        nick_name = "training_user"
        gender_code = ""
        age_range_code = 0
        current_participant_uid = ""
        print(">>> PC_MODE=training — App_Input optional, using placeholder data")
        app_input = App_Input()
        app_input.mainloop()
    else:
        app_input = App_Input()
        app_input.mainloop()
except Exception:
    print(">>> Failed while running input window. Full traceback:")
    traceback.print_exc()
    raise SystemExit(1)




#>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> Setup GUI Video

#PATH_VIDEO = r"C:\Users\50575\Desktop\BDT\VIDEO_EXPERIMENT\TLL\TOP_VIEW\anom_02.mp4"
#cap = cv2.VideoCapture(PATH_VIDEO)          #0, cv2.CAP_DSHOW
import platform
if platform.system() == 'Windows':
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
else:  # Linux/Mac
    cap = cv2.VideoCapture(0)  # atau cv2.CAP_V4L2

cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)      #1280   640
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)     #720    480

print(">>> Start Test ...")

class TextFrame(customtkinter.CTkFrame):

    def __init__(self, master, title): #, values
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.title_str = title
        self.title_label = customtkinter.CTkLabel(
            self, text=self.title_str, fg_color="transparent",
            text_color=_CLR_MUTED, font=(_FONT_PRIMARY[0], 14, "bold"), corner_radius=0,
        )
        self.title_label.grid(row=0, column=0, padx=0, pady=(0, 8), sticky="w")


class ImageFrame(customtkinter.CTkFrame):

    def __init__(self, master, title, border_width=0, border_color=_CLR_BORDER):
        super().__init__(master, border_width=border_width, border_color=border_color, fg_color=_CLR_BG, corner_radius=_CORNER_RADIUS)
        self.grid_columnconfigure(0, weight=1)
        self.title_str = title
        self.title_label = customtkinter.CTkLabel(
            self, text=self.title_str, fg_color="transparent",
            text_color=_CLR_MUTED, font=(_FONT_PRIMARY[0], 13), corner_radius=0,
        )
        self.title_label.grid(row=0, column=0, padx=0, pady=(0, 8), sticky="w")



class YOLODetectionThread(threading.Thread):
    """Background thread for YOLO inference to prevent GUI blocking"""
    def __init__(self, model, use_bantal_model):
        super().__init__(daemon=True)
        self.model = model
        self.use_bantal_model = use_bantal_model
        self.frame_queue = Queue(maxsize=1)  # Reduce to 1 for less memory
        self.result_queue = Queue(maxsize=1)
        self.running = True
        self.inference_count = 0
        self.last_inference_time = 0
        print(">>> YOLO thread initialized")
        
    def run(self):
        print(">>> YOLO thread started")
        while self.running:
            try:
                if not self.frame_queue.empty():
                    frame = self.frame_queue.get(timeout=0.01)
                    
                    # Measure inference time
                    start_time = time.time()
                    
                    # YOLO inference
                    if self.use_bantal_model:
                        results = self.model(frame, verbose=False)
                        
                        # Convert Ultralytics results to list format
                        if len(results) > 0 and hasattr(results[0], 'boxes'):
                            boxes = results[0].boxes
                            detections = []
                            
                            for box in boxes:
                                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                                conf = box.conf[0].cpu().numpy()
                                cls = int(box.cls[0].cpu().numpy())
                                detections.append([x1, y1, x2, y2, conf, cls, ''])
                            
                            list_tracked_objects = detections
                        else:
                            list_tracked_objects = []
                    else:
                        results = self.model(frame)
                        df_tracked_objects = results.pandas().xyxy[0]
                        list_tracked_objects = df_tracked_objects.values.tolist()
                    
                    self.last_inference_time = (time.time() - start_time) * 1000
                    self.inference_count += 1
                    
                    # Debug output every 30 inferences
                    if self.inference_count % 30 == 0:
                        print(f">>> YOLO: {self.inference_count} inferences, last: {self.last_inference_time:.1f}ms")
                    
                    # Put results in queue (remove old if full)
                    if self.result_queue.full():
                        try:
                            self.result_queue.get_nowait()
                        except:
                            pass
                    self.result_queue.put(list_tracked_objects)
                else:
                    time.sleep(0.001)
            except Exception as e:
                print(f"YOLO thread error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(0.01)
    
    def stop(self):
        self.running = False
        print(">>> YOLO thread stopped")


class SerialReaderThread(threading.Thread):
    """Background thread for reading serial data from ESP32"""
    def __init__(self, port=None, baudrate=115200):
        super().__init__(daemon=True)
        self.port = port
        self.baudrate = baudrate
        self.serial_conn = None
        self.running = True
        self.message_queue = Queue(maxsize=10)
        
        # Auto-detect ESP32 port if not specified
        if not self.port:
            self.port = self.find_esp32_port()
        
        if self.port:
            try:
                self.serial_conn = serial.Serial(self.port, self.baudrate, timeout=0.1)
                print(f">>> Serial connected to {self.port} at {self.baudrate} baud")
            except Exception as e:
                print(f">>> Failed to open serial port {self.port}: {e}")
                self.serial_conn = None
        else:
            print(">>> No ESP32 device found")
    
    def find_esp32_port(self):
        """Auto-detect ESP32 COM port (Windows/Linux)"""
        ports = serial.tools.list_ports.comports()
        
        # Check for Linux Bluetooth RFCOMM first
        import platform
        if platform.system() == 'Linux':
            # Check for /dev/rfcomm0 (Bluetooth)
            if os.path.exists('/dev/rfcomm0'):
                print(f">>> Found Bluetooth RFCOMM at /dev/rfcomm0")
                return '/dev/rfcomm0'
        
        # Check standard serial ports
        for port in ports:
            # ESP32 identifiers (USB or Bluetooth)
            identifiers = ['USB', 'CH340', 'CP210', 'UART', 'Serial', 'Bluetooth']
            if any(id in port.description for id in identifiers):
                print(f">>> Found potential ESP32 at {port.device}: {port.description}")
                return port.device
        
        return None
    
    def run(self):
        if not self.serial_conn:
            print(">>> Serial reader thread: No connection available")
            return
            
        print(">>> Serial reader thread started")
        while self.running:
            try:
                if self.serial_conn and self.serial_conn.in_waiting > 0:
                    line = self.serial_conn.readline().decode('utf-8').strip()
                    if line:
                        # Put message in queue
                        if not self.message_queue.full():
                            self.message_queue.put(line)
                        if line == "disable_image":
                            print(f">>> Serial received: {line}")
                time.sleep(0.01)
            except Exception as e:
                print(f">>> Serial read error: {e}")
                time.sleep(0.1)
    
    def get_message(self):
        """Get message from queue (non-blocking)"""
        try:
            return self.message_queue.get_nowait()
        except:
            return None
    
    def stop(self):
        self.running = False
        if self.serial_conn:
            self.serial_conn.close()
        print(">>> Serial reader thread stopped")


class TimeIn(customtkinter.CTk):

    def report_callback_exception(self, exc, val, tb):
        """Capture tkinter callback exceptions with full traceback."""
        log_exception("Tk callback exception (TimeIn)", exc, val, tb)

    def __init__(self):

        super().__init__()
        self.title("Block Design Test")
        self.configure(fg_color=_CLR_BG)

        # Set window size based on display mode
        if DISPLAY_HALF:
            self.geometry("960x560")
        else:
            self.geometry("1200x800")  # Larger window for full display mode
        
        # Configure main grid with different weights based on display mode
        if DISPLAY_HALF:
            # Half display mode (4:1 ratio for top:bottom)
            self.grid_rowconfigure(0, weight=4)  # Top part (larger portion)
            self.grid_rowconfigure(1, weight=1)  # Bottom part (smaller portion)
            self.grid_columnconfigure(0, weight=1)
            self.grid_columnconfigure(1, weight=1)
            
            # Create a container for the top half
            self.top_container = customtkinter.CTkFrame(self, fg_color=_CLR_BG)
            self.top_container.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 150))
            self.top_container.grid_columnconfigure(0, weight=1)
            self.top_container.grid_columnconfigure(1, weight=1)
            self.top_container.grid_rowconfigure(0, weight=1)
            self.top_container.grid_rowconfigure(1, weight=0)  # For the button row
            
            # Content container for half display
            self.content_container = customtkinter.CTkFrame(self.top_container, fg_color=_CLR_BG)
            self.content_container.grid(row=0, column=0, columnspan=2, sticky="nsew")
            self.content_container.grid_columnconfigure(0, weight=1)
            self.content_container.grid_columnconfigure(1, weight=1)
            self.content_container.grid_rowconfigure(0, weight=1)
        else:
            # Full display mode (use full window)
            self.grid_rowconfigure(0, weight=1)
            self.grid_columnconfigure(0, weight=1)
            self.grid_columnconfigure(1, weight=1)
            
            # Main container for full display
            self.top_container = customtkinter.CTkFrame(self, fg_color=_CLR_BG)
            self.top_container.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=10, pady=10)
            self.top_container.grid_columnconfigure(0, weight=1)
            self.top_container.grid_columnconfigure(1, weight=1)
            self.top_container.grid_rowconfigure(0, weight=1)
            
            # Content container for full display
            self.content_container = customtkinter.CTkFrame(self.top_container, fg_color=_CLR_BG)
            self.content_container.grid(row=0, column=0, columnspan=2, sticky="nsew")
            self.content_container.grid_columnconfigure(0, weight=1)
            self.content_container.grid_columnconfigure(1, weight=1)
            self.content_container.grid_rowconfigure(0, weight=1)

        # --------------------------------------------------- Design Section

        # Left side container for timer and design
        self.left_side = customtkinter.CTkFrame(self.content_container, fg_color=_CLR_BG)
        
        # If camera is hidden, center the left_side by using columnspan
        if HIDE_CAMERA:
            # Center the design section by spanning both columns
            if DISPLAY_HALF:
                self.left_side.grid(row=0, column=0, columnspan=2, sticky="", padx=10, pady=10)
            else:
                self.left_side.grid(row=0, column=0, columnspan=2, sticky="", padx=20, pady=20)
        else:
            # Normal layout with camera on the right
            if DISPLAY_HALF:
                self.left_side.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
            else:
                self.left_side.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)
            
        self.left_side.grid_rowconfigure(1, weight=1)
        self.left_side.grid_columnconfigure(1, weight=1)

        # ── Level Indicator ──────────────────────────────────────────────────
        self.level_text = customtkinter.CTkLabel(
            self.left_side, text="LEVEL 1 / 8",
            font=(_FONT_PRIMARY[0], 14, "bold"), text_color=_CLR_ACCENT,
        )
        self.level_text.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        # Timer frame — Modern: rounded, accent border, dark fill
        timer_width = 120 if DISPLAY_HALF else 150
        timer_font_size = 28 if DISPLAY_HALF else 32
        
        self.timer_frame = customtkinter.CTkFrame(
            self.left_side, corner_radius=_CORNER_RADIUS, fg_color=_CLR_BG,
            border_width=2, border_color=_CLR_ACCENT,
            width=timer_width,
        )
        self.timer_frame.grid(row=1, column=0, rowspan=2, padx=(0, 14), pady=10, sticky="ns")
        self.timer_frame.pack_propagate(False)

        customtkinter.CTkLabel(
            self.timer_frame,
            text="WAKTU",
            font=(_FONT_PRIMARY[0], 11, "bold"),
            text_color=_CLR_MUTED,
        ).pack(pady=(16, 0))

        self.timer_label = customtkinter.CTkLabel(
            self.timer_frame,
            text="00:00",
            font=(_FONT_PRIMARY[0], timer_font_size, "bold"),
            text_color=_CLR_ACCENT,
        )
        self.timer_label.pack(pady=(6, 4))
        self.timer_running = False
        self.start_time = 0
        
        # Danger bar — Modern: rounded, accent progress
        self.danger_bar = customtkinter.CTkProgressBar(
            self.timer_frame, width=timer_width - 24, height=10, corner_radius=5
        )
        self.danger_bar.pack(pady=(0, 18))
        self.danger_bar.set(0.0)
        self.danger_bar.configure(progress_color=_CLR_ACCENT, fg_color=_CLR_SUBTLE_BORDER)

        # ── Level Progress Badges ─────────────────────────────────────────────
        self.level_badge_frame = customtkinter.CTkFrame(self.left_side, fg_color="transparent")
        self.level_badge_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.level_badge_frame.grid_columnconfigure(tuple(range(8)), weight=1)
        self.level_badges = []
        for i in range(8):
            badge = customtkinter.CTkLabel(
                self.level_badge_frame, text=str(i + 1),
                font=(_FONT_PRIMARY[0], 11, "bold"),
                width=30, height=30,
                fg_color=_CLR_CARD, text_color=_CLR_MUTED,
                corner_radius=_CORNER_RADIUS_SMALL,
            )
            badge.grid(row=0, column=i, padx=3, pady=3)
            self.level_badges.append(badge)

        # Star frame (legacy, kept for compatibility — hidden by default)
        self.star_frame = customtkinter.CTkFrame(self.left_side, fg_color="transparent")
        self.star_labels = []
        for i in range(8):
            star = customtkinter.CTkLabel(self.star_frame, text="", font=(_FONT_PRIMARY[0], 10), text_color=_CLR_MUTED)
            self.star_labels.append(star)
        self.star_frame.grid_remove()

        # Configure column weights for left_side (block design) and content_container (camera)
        self.left_side.grid_columnconfigure(1, weight=1)  # Block design column
        
        if not HIDE_CAMERA:
            self.content_container.grid_columnconfigure(1, weight=3)  # Camera column (3x wider than block design)
        else:
            # When camera is hidden, make both columns equal weight for centering
            self.content_container.grid_columnconfigure(0, weight=1)
            self.content_container.grid_columnconfigure(1, weight=1)

        # Design frames next to the timer
        self.design_frame_0 = TextFrame(self.left_side, "Block Design")
        self.design_frame_0.grid(row=0, column=1, padx=0, pady=(0, 5), sticky="nsew")

        self.design_frame_1 = ImageFrame(self.left_side, "", border_width=3, border_color=_CLR_BORDER)
        self.design_frame_1.grid(row=1, column=1, padx=0, pady=0, sticky="nsew")
        self.design_frame_1.grid_propagate(False)

        # Results dashboard (hidden until test ends)
        self.results_frame = customtkinter.CTkFrame(self.left_side, fg_color=_CLR_CARD, corner_radius=_CORNER_RADIUS, border_width=1, border_color=_CLR_SUBTLE_BORDER)
        self.results_frame.grid(row=1, column=1, padx=0, pady=0, sticky="nsew")
        self.results_frame.grid_columnconfigure(0, weight=1)
        self.results_frame.grid_remove()

        # --------------------------------------------------- Camera Section

        # Only create camera section if HIDE_CAMERA is False
        if not HIDE_CAMERA:
            camera_padx = 10 if DISPLAY_HALF else 20
            camera_pady = (40, 10) if DISPLAY_HALF else (50, 20)
            
            self.video_frame_0 = TextFrame(self.content_container, "Upper Table Camera")
            self.video_frame_0.grid(row=0, column=1, padx=camera_padx, pady=0, sticky="nsew")

            # Detection status pill
            self.detection_status = customtkinter.CTkLabel(
                self.video_frame_0, text="Mencari tangan...",
                font=(_FONT_PRIMARY[0], 10, "bold"),
                text_color=_CLR_ACCENT, fg_color=_CLR_CARD,
                corner_radius=_CORNER_RADIUS_PILL, width=140, height=26,
            )
            self.detection_status.grid(row=0, column=1, padx=(0, 10), pady=(10, 0), sticky="e")

            # Camera settings button (next to title)
            self.camera_settings_btn = customtkinter.CTkButton(
                self.video_frame_0, text="Set",
                font=(_FONT_PRIMARY[0], 10, "bold"), width=40, height=32,
                fg_color=_CLR_SUBTLE_BORDER, hover_color=_CLR_ACCENT,
                text_color=_CLR_TEXT,
                corner_radius=_CORNER_RADIUS_SMALL, command=self._open_camera_settings
            )
            self.camera_settings_btn.grid(row=0, column=2, padx=(0, 10), pady=(10, 0))

            self.video_frame_1 = ImageFrame(self.content_container, "", border_width=3, border_color=_CLR_BORDER)
            self.video_frame_1.grid(row=0, column=1, padx=camera_padx, pady=camera_pady, sticky="nsew")
            self.video_frame_1.grid_propagate(False)
        else:
            # Camera hidden - set to None
            self.video_frame_0 = None
            self.video_frame_1 = None
            print(">>> HIDE_CAMERA enabled - Camera section hidden")
        
        # Adjust image sizes to fit the frames
        if DISPLAY_HALF:
            self.frame_width = 400
            self.frame_height = 400
        else:
            self.frame_width = 600
            self.frame_height = 600
        
        # Load and resize the initial image
        IMAGE_PATH = os.path.join(BASE_DIR, 'FILES', 'TEST_1000x1000', '0Bx.jpg')
        self.image = customtkinter.CTkImage(
            light_image=Image.open(IMAGE_PATH).resize((self.frame_width, self.frame_height), Image.Resampling.LANCZOS),
            size=(self.frame_width, self.frame_height)
        )
        # Create the label once and store it as an instance variable
        self.image_label = customtkinter.CTkLabel(master=self.design_frame_1, text='')
        self.image_label.pack(expand=True, fill="both")
        self.image_label.configure(image=self.image)

        # ── Countdown Overlay ─────────────────────────────────────────────────
        self.countdown_overlay = customtkinter.CTkLabel(
            self, text="",
            font=(_FONT_PRIMARY[0], 80, "bold"),
            text_color=_CLR_ACCENT, fg_color="transparent",
        )
        
        # ── Celebration Flash Overlay ─────────────────────────────────────────
        self.celebration_overlay = customtkinter.CTkFrame(
            self.design_frame_1, fg_color=_CLR_SUCCESS, corner_radius=_CORNER_RADIUS
        )
        self.celebration_label = customtkinter.CTkLabel(
            self.celebration_overlay, text="",
            font=(_FONT_PRIMARY[0], 38, "bold"), text_color=_CLR_BG,
        )
        self.celebration_label.place(relx=0.5, rely=0.5, anchor="center")
        
        # Camera label - Configure grid for video_frame_1 (only if camera not hidden)
        if not HIDE_CAMERA:
            self.video_frame_1.grid_rowconfigure(0, weight=1)
            self.video_frame_1.grid_columnconfigure(0, weight=1)
            
            # Create camera label with grid
            self.camera = customtkinter.CTkLabel(self.video_frame_1, text="", anchor="center")
            self.camera.grid(row=0, column=0, sticky="nsew")
        else:
            # Camera hidden - set to None
            self.camera = None

        # Add the start button
        button_font_size = 30 if DISPLAY_HALF else 36
        button_pady = 10 if DISPLAY_HALF else 20
        
        self.button_0 = customtkinter.CTkButton(
            self.top_container,
            text="MULAI TES",
            font=(_FONT_PRIMARY[0], button_font_size, "bold"),
            fg_color=_CLR_ACCENT,
            hover_color=_CLR_ACCENT2,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS,
            height=64,
            command=self.button_0_callback,
        )
        if DISPLAY_HALF:
            self.button_0.grid(row=1, column=0, columnspan=2, padx=20, pady=button_pady, sticky="ew")
        else:
            # In full mode, place the button at the bottom of the left panel
            self.button_0.grid(row=2, column=0, columnspan=2, padx=20, pady=button_pady, sticky="ew")

        # Status bar (half display mode — shows level info) — Modern pill style
        if DISPLAY_HALF:
            self.bottom_container = customtkinter.CTkFrame(
                self, fg_color=_CLR_CARD, height=44,
                border_width=1, border_color=_CLR_SUBTLE_BORDER,
            )
            self.bottom_container.grid(row=1, column=0, columnspan=2, sticky="ew")
            self.bottom_container.grid_propagate(False)
            self.status_label = customtkinter.CTkLabel(
                self.bottom_container,
                text=f"MODE: {PC_MODE.upper()}  |  LEVEL 1/{MAX_LEVEL}",
                font=(_FONT_PRIMARY[0], 12, "bold"), text_color=_CLR_ACCENT, anchor="w",
            )
            self.status_label.pack(side="left", padx=20, pady=8)

        #self.start_zero = time.time()
        #self.start_thumb = time.time()

        self.timer_task_all = []

        self.timer_task_01 = 0
        self.timer_task_02 = 0
        self.timer_task_03 = 0
        self.timer_task_04 = 0
        self.timer_task_05 = 0
        self.timer_task_06 = 0
        self.timer_task_07 = 0
        self.timer_task_08 = 0

        self.task_flag_01 = True
        self.task_flag_02 = True
        self.task_flag_03 = True
        self.task_flag_04 = True
        self.task_flag_05 = True
        self.task_flag_06 = True
        self.task_flag_07 = True
        self.task_flag_08 = True

        self.current_question = 1

        self.cognitive_age_list = []

        global nick_name
        global gender_code
        global age_range_code

        self.nick_name = nick_name
        self.gender_code = gender_code
        self.age_range_code = age_range_code

        self.retry_button = None

        #self.task_state = 1

        # Set the maximum level from environment variable
        self.max_level = MAX_LEVEL
        
        # Bind Enter key to skip level
        self.bind('<Return>', self.skip_current_level)
        
        # Initialize YOLO detection thread
        self.yolo_thread = YOLODetectionThread(model_yolo, USE_BANTAL_MODEL)
        self.yolo_thread.start()
        self.latest_detections = []
        self.frame_count = 0
        self.yolo_skip_frames = int(os.getenv('YOLO_SKIP_FRAMES', '2'))  # Process every 3rd frame
        
        # Initialize MediaPipe Hands once (not every frame)
        self.mp_hands_detector = None
        if mp_hands is not None:
            try:
                self.mp_hands_detector = mp_hands.Hands(
                    min_detection_confidence=0.5,
                    min_tracking_confidence=0.5,
                    max_num_hands=2
                )
            except Exception as e:
                print(f">>> Failed to initialize MediaPipe Hands: {e}. Hand landmark detection disabled.")
        
        # FPS monitoring
        self.fps_counter = 0
        self.fps_start_time = time.time()
        self.current_fps = 0
        
        # Skip MediaPipe on same frames as YOLO for better performance
        self.mediapipe_skip_frames = int(os.getenv('MEDIAPIPE_SKIP_FRAMES', '2'))
        
        # Debug mode
        self.debug_mode = os.getenv('DEBUG_MODE', 'false').lower() == 'true'
        
        # Button mode for image display control
        self.button_mode = BUTTON_MODE
        self.image_visible = False
        self.image_show_time = None
        self.image_display_duration = 5.0  # 5 seconds
        
        # Serial communication thread for button mode
        self.serial_thread = None
        if self.button_mode:
            print(">>> BUTTON_MODE enabled - Image will show for 5 seconds then hide")
            self.serial_thread = SerialReaderThread()
            self.serial_thread.start()
        
        # Cache for level images
        self.cached_level_images = {}
        self.preload_level_images()
        

        self._api_event("join_room")
        if IS_MULTIPLAYER:
            print(f">>> [API] Joined room '{ROOM_ID}' as player {PLAYER_NUM} ('{self.nick_name}')")

    def _api_event(self, event_type: str, level: int = 0, time_sec: float = 0.0):
        if not IS_MULTIPLAYER:
            return
        _fire_event({
            "type":        event_type,
            "room_id":     CURRENT_ROOM_CODE or ROOM_ID,
            "player_name": self.nick_name,
            "player_num":  PLAYER_NUM,
            "level":       level,
            "time_sec":    round(float(time_sec), 3),
        })

    def _start_heartbeat(self):
        """Send heartbeat every 15s in competition mode to keep room alive."""
        if not IS_MULTIPLAYER:
            return
        def _loop():
            fail_count = 0
            while getattr(self, '_heartbeat_active', True):
                time.sleep(15)
                ok = _api_post(
                    f"{API_SERVER_URL}/api/game/event",
                    {
                        "type":        "heartbeat",
                        "room_id":     CURRENT_ROOM_CODE or ROOM_ID,
                        "player_name": self.nick_name,
                        "player_num":  PLAYER_NUM,
                    },
                    timeout=5,
                )
                if ok:
                    fail_count = 0
                else:
                    fail_count += 1
                    if fail_count >= 3:
                        print(f">>> [API] Heartbeat failed 3x — connection lost")
                        break
        self._heartbeat_active = True
        threading.Thread(target=_loop, daemon=True).start()

    def _stop_heartbeat(self):
        self._heartbeat_active = False

    def preload_level_images(self):
        """Preload and cache all level images to avoid I/O lag during gameplay"""
        print(">>> Preloading level images...")
        for variant, path in LEVEL_PATHS.items():
            try:
                img = Image.open(path)
                # Cache resized version for display
                self.cached_level_images[variant] = img.resize(
                    (self.frame_width, self.frame_height), 
                    Image.Resampling.LANCZOS
                )
            except Exception as e:
                print(f"Error loading {variant}: {e}")
        print(f">>> Cached {len(self.cached_level_images)} level images")
    
    def get_cached_level_image(self, variant):
        """Get cached level image or load if not cached"""
        if variant in self.cached_level_images:
            return self.cached_level_images[variant]
        else:
            # Fallback: load on demand
            try:
                img = Image.open(LEVEL_PATHS[variant])
                return img.resize((self.frame_width, self.frame_height), Image.Resampling.LANCZOS)
            except Exception as e:
                print(f"Error loading {variant}: {e}")
                return None

    def end_test(self):
        self._stop_heartbeat()
        self.current_question = 9
        self.reset_timer()
        self.stop_timer()
        self.current_level_button.grid_remove()
        self.timer_task_avg = format(float(sum(self.timer_task_all) / len(self.timer_task_all)), ".3f")
        cognitive_age_avg = int(sum(self.cognitive_age_list) / len(self.cognitive_age_list))
        age_real = int(self.age_range_code)
        age_cog = cognitive_age_avg

        visuo_spatial = 100

        if age_cog <= age_real:
            visuo_spatial = 100 
        else:
            visuo_spatial = 100 - (age_cog - age_real)

        print("Your name is " + self.nick_name)
        print("Your gender is " + self.gender_code)
        print("Your Average Completed Time is " + str(self.timer_task_avg) + " second")
        print("Your Current Age is " + str(age_real) + " years")
        print("Your Cognitive Age is " + str(age_cog) + " years")
        print("Your Cognitive Fitness is " + str(visuo_spatial) + " %")

        play_audio("AUDIO/selesai.wav")
        time.sleep(3)

        # Save participant result to API server
        try:
            global current_participant_uid

            name = self.nick_name
            real_age = int(self.age_range_code)
            gender = self.gender_code
            estimated_age = int(age_cog)
            avg_time_all = float(self.timer_task_avg)
            times = self.timer_task_all
            uid = current_participant_uid

            payload = {
                "uid":           uid,
                "mode":          PC_MODE,
                "nick_name":     name,
                "gender":        gender,
                "age":           real_age,
                "task_01":       float(times[0]) if len(times) > 0 else None,
                "task_02":       float(times[1]) if len(times) > 1 else None,
                "task_03":       float(times[2]) if len(times) > 2 else None,
                "task_04":       float(times[3]) if len(times) > 3 else None,
                "task_05":       float(times[4]) if len(times) > 4 else None,
                "task_06":       float(times[5]) if len(times) > 5 else None,
                "task_07":       float(times[6]) if len(times) > 6 else None,
                "task_08":       float(times[7]) if len(times) > 7 else None,
                "task_avg":      avg_time_all,
                "cognitive_age": estimated_age,
                "visuo_spatial": visuo_spatial,
            }

            if uid:
                _submit_results(uid, payload)
            else:
                _save_training_locally(payload)
                print(f">>> [LOCAL] Training results saved without UID")

            # Tournament cup: report match finished with score
            if TOURNAMENT_MODE and CURRENT_ROOM_CODE:
                player_num = 1 if TOURNAMENT_IS_P1 else 2
                score = float(visuo_spatial)
                _send_tournament_event(
                    CURRENT_ROOM_CODE,
                    "match_finished",
                    player_num=player_num,
                    score=score,
                )
                print(f">>> [TOURNAMENT] Match finished — Player {player_num} score: {score}")

        except Exception as e:
            print(f">>> [API] Error saving participant: {e}")

        self._build_results_dashboard(age_real, age_cog, visuo_spatial)

    def _build_results_dashboard(self, age_real, age_cog, visuo_spatial):
        """Show a celebratory results card with scores and per-level breakdown."""
        # Hide game elements, show results
        self.design_frame_0.grid_remove()
        self.design_frame_1.grid_remove()
        self.level_badge_frame.grid_remove()
        self.star_frame.grid_remove()
        self.results_frame.grid()

        # Clear previous results
        for w in self.results_frame.winfo_children():
            w.destroy()

        # Trophy header
        customtkinter.CTkLabel(
            self.results_frame, text="TES SELESAI",
            font=(_FONT_PRIMARY[0], 26, "bold"), text_color=_CLR_TEXT,
        ).grid(row=0, column=0, pady=(20, 6))

        # Refined accent line
        line = customtkinter.CTkFrame(self.results_frame, fg_color=_CLR_ACCENT, height=3, corner_radius=0)
        line.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 16))

        # Main score card — Modern: rounded, accent border, dark fill
        trophy = customtkinter.CTkFrame(
            self.results_frame, fg_color=_CLR_BG, corner_radius=_CORNER_RADIUS,
            border_width=2, border_color=_CLR_ACCENT,
        )
        trophy.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 16))
        trophy.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(
            trophy, text=f"{visuo_spatial}%",
            font=(_FONT_PRIMARY[0], 44, "bold"), text_color=_CLR_ACCENT,
        ).grid(row=0, column=0, pady=(18, 2))
        customtkinter.CTkLabel(
            trophy, text="Kebugaran Visual-Spasial",
            font=(_FONT_PRIMARY[0], 13), text_color=_CLR_MUTED,
        ).grid(row=1, column=0, pady=(0, 18))

        # Stats row
        stats = customtkinter.CTkFrame(self.results_frame, fg_color="transparent")
        stats.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 12))
        stats.grid_columnconfigure((0, 1), weight=1)

        def _stat_card(parent, title, value):
            c = customtkinter.CTkFrame(parent, fg_color=_CLR_CARD, corner_radius=_CORNER_RADIUS, border_width=1, border_color=_CLR_SUBTLE_BORDER)
            c.grid_columnconfigure(0, weight=1)
            customtkinter.CTkLabel(c, text=title, font=(_FONT_PRIMARY[0], 12), text_color=_CLR_MUTED).grid(row=0, column=0, pady=(12, 0))
            customtkinter.CTkLabel(c, text=value, font=(_FONT_PRIMARY[0], 20, "bold"), text_color=_CLR_TEXT).grid(row=1, column=0, pady=(0, 12))
            return c

        _stat_card(stats, "Usia", f"{age_real} th").grid(row=0, column=0, padx=(0, 8), sticky="ew")
        _stat_card(stats, "Usia Kognitif", f"{age_cog} th").grid(row=0, column=1, padx=(8, 0), sticky="ew")

        # Per-level times
        if self.timer_task_all:
            customtkinter.CTkLabel(
                self.results_frame, text="Waktu per Level",
                font=(_FONT_PRIMARY[0], 13, "bold"), text_color=_CLR_MUTED,
            ).grid(row=4, column=0, pady=(12, 6))
            times_frame = customtkinter.CTkFrame(self.results_frame, fg_color="transparent")
            times_frame.grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 12))
            cols = min(len(self.timer_task_all), 4)
            for i in range(cols):
                times_frame.grid_columnconfigure(i, weight=1)
            for i, t in enumerate(self.timer_task_all):
                r, c = divmod(i, cols)
                customtkinter.CTkLabel(
                    times_frame, text=f"L{i+1}: {t:.1f}s",
                    font=(_FONT_PRIMARY[0], 12), text_color=_CLR_TEXT,
                ).grid(row=r, column=c, pady=3)

        # Buttons
        btn_frame = customtkinter.CTkFrame(self.results_frame, fg_color="transparent")
        btn_frame.grid(row=6, column=0, sticky="ew", padx=20, pady=(10, 20))
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)

        customtkinter.CTkButton(
            btn_frame, text="Main Lagi",
            font=(_FONT_PRIMARY[0], 14, "bold"),
            fg_color=_CLR_ACCENT, hover_color=_CLR_ACCENT2,
            text_color=_CLR_TEXT, corner_radius=_CORNER_RADIUS, height=56,
            command=self.retry_test,
        ).grid(row=0, column=0, padx=(0, 8), sticky="ew")
        customtkinter.CTkButton(
            btn_frame, text="Salin Hasil",
            font=(_FONT_PRIMARY[0], 14, "bold"),
            fg_color=_CLR_CARD, hover_color=_CLR_SUBTLE_BORDER,
            text_color=_CLR_TEXT, corner_radius=_CORNER_RADIUS, height=56,
        ).grid(row=0, column=1, padx=(8, 0), sticky="ew")
    
    def skip_current_level(self, event=None):
        """Skip the current level when Enter key is pressed"""
        if not hasattr(self, 'current_question') or not hasattr(self, 'timer_running') or not self.timer_running:
            return
            
        current_time = time.time()
        time_elapsed = round((current_time - self.start_task), 2)
        
        print(f"Skipping level {self.current_question} after {time_elapsed} seconds")
        
        # Record the actual time taken for the level
        if self.current_question == 1 and self.task_flag_01:
            self.timer_task_01 = time_elapsed
            self.timer_task_all.append(self.timer_task_01)
            self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_01))
            self.task_flag_01 = False
            print(f"TASK 1 SKIPPED after {self.timer_task_01} seconds")
            if int(self.timer_task_01) < 10:
                play_audio("AUDIO/menakjubkan.wav")
            elif int(self.timer_task_01) < 15:
                play_audio("AUDIO/hebat_sekali.wav")
            elif int(self.timer_task_01) < 20:
                play_audio("AUDIO/mantap.wav")
            elif int(self.timer_task_01) < 25:
                play_audio("AUDIO/kerja_bagus.wav")
            elif int(self.timer_task_01) < 30:
                play_audio("AUDIO/ayo_semangat.wav")
            else:
                play_audio("AUDIO/jangan_menyerah.wav")
            if self.max_level != 1:
                play_audio("AUDIO/lanjut_lvl2.wav")
            self.current_question = 2
            
        elif self.current_question == 2 and self.task_flag_02:
            self.timer_task_02 = time_elapsed
            self.timer_task_all.append(self.timer_task_02)
            self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_02))
            self.task_flag_02 = False
            print(f"TASK 2 SKIPPED after {self.timer_task_02} seconds")
            if int(self.timer_task_02) < 10:
                play_audio("AUDIO/menakjubkan.wav")
            elif int(self.timer_task_02) < 15:
                play_audio("AUDIO/hebat_sekali.wav")
            elif int(self.timer_task_02) < 20:
                play_audio("AUDIO/mantap.wav")
            elif int(self.timer_task_02) < 25:
                play_audio("AUDIO/kerja_bagus.wav")
            elif int(self.timer_task_02) < 30:
                play_audio("AUDIO/ayo_semangat.wav")
            else:
                play_audio("AUDIO/jangan_menyerah.wav")
            if self.max_level != 2:
                play_audio("AUDIO/lanjut_lvl3.wav")
            self.current_question = 3
            
        # Add more levels as needed (up to 8)
        elif self.current_question == 3 and self.task_flag_03:
            self.timer_task_03 = time_elapsed
            self.timer_task_all.append(self.timer_task_03)
            self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_03))
            self.task_flag_03 = False
            print(f"TASK 3 SKIPPED after {self.timer_task_03} seconds")
            if int(self.timer_task_03) < 10:
                play_audio("AUDIO/menakjubkan.wav")
            elif int(self.timer_task_03) < 15:
                play_audio("AUDIO/hebat_sekali.wav")
            elif int(self.timer_task_03) < 20:
                play_audio("AUDIO/mantap.wav")
            elif int(self.timer_task_03) < 25:
                play_audio("AUDIO/kerja_bagus.wav")
            elif int(self.timer_task_03) < 30:
                play_audio("AUDIO/ayo_semangat.wav")
            else:
                play_audio("AUDIO/jangan_menyerah.wav")
            if self.max_level != 3:
                play_audio("AUDIO/lanjut_lvl4.wav")
            self.current_question = 4
            
        elif self.current_question == 4 and self.task_flag_04:
            self.timer_task_04 = time_elapsed
            self.timer_task_all.append(self.timer_task_04)
            self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_04))
            self.task_flag_04 = False
            print(f"TASK 4 SKIPPED after {self.timer_task_04} seconds")
            if int(self.timer_task_04) < 10:
                play_audio("AUDIO/menakjubkan.wav")
            elif int(self.timer_task_04) < 15:
                play_audio("AUDIO/hebat_sekali.wav")
            elif int(self.timer_task_04) < 20:
                play_audio("AUDIO/mantap.wav")
            elif int(self.timer_task_04) < 25:
                play_audio("AUDIO/kerja_bagus.wav")
            elif int(self.timer_task_04) < 30:
                play_audio("AUDIO/ayo_semangat.wav")
            else:
                play_audio("AUDIO/jangan_menyerah.wav")
            if self.max_level != 4:
                play_audio("AUDIO/lanjut_lvl5.wav")
            self.current_question = 5
            
        elif self.current_question == 5 and self.task_flag_05:
            self.timer_task_05 = time_elapsed
            self.timer_task_all.append(self.timer_task_05)
            self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_05))
            self.task_flag_05 = False
            print(f"TASK 5 SKIPPED after {self.timer_task_05} seconds")
            if int(self.timer_task_05) < 10:
                play_audio("AUDIO/menakjubkan.wav")
            elif int(self.timer_task_05) < 15:
                play_audio("AUDIO/hebat_sekali.wav")
            elif int(self.timer_task_05) < 20:
                play_audio("AUDIO/mantap.wav")
            elif int(self.timer_task_05) < 25:
                play_audio("AUDIO/kerja_bagus.wav")
            elif int(self.timer_task_05) < 30:
                play_audio("AUDIO/ayo_semangat.wav")
            else:
                play_audio("AUDIO/jangan_menyerah.wav")
            if self.max_level != 5:
                play_audio("AUDIO/lanjut_lvl6.wav")
            self.current_question = 6
            
        elif self.current_question == 6 and self.task_flag_06:
            self.timer_task_06 = time_elapsed
            self.timer_task_all.append(self.timer_task_06)
            self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_06))
            self.task_flag_06 = False
            print(f"TASK 6 SKIPPED after {self.timer_task_06} seconds")
            if int(self.timer_task_06) < 10:
                play_audio("AUDIO/menakjubkan.wav")
            elif int(self.timer_task_06) < 15:
                play_audio("AUDIO/hebat_sekali.wav")
            elif int(self.timer_task_06) < 20:
                play_audio("AUDIO/mantap.wav")
            elif int(self.timer_task_06) < 25:
                play_audio("AUDIO/kerja_bagus.wav")
            elif int(self.timer_task_06) < 30:
                play_audio("AUDIO/ayo_semangat.wav")
            else:
                play_audio("AUDIO/jangan_menyerah.wav")
            if self.max_level != 6:
                play_audio("AUDIO/lanjut_lvl7.wav")
            self.current_question = 7
            
        elif self.current_question == 7 and self.task_flag_07:
            self.timer_task_07 = time_elapsed
            self.timer_task_all.append(self.timer_task_07)
            self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_07))
            self.task_flag_07 = False
            print(f"TASK 7 SKIPPED after {self.timer_task_07} seconds")
            if int(self.timer_task_07) < 10:
                play_audio("AUDIO/menakjubkan.wav")
            elif int(self.timer_task_07) < 15:
                play_audio("AUDIO/hebat_sekali.wav")
            elif int(self.timer_task_07) < 20:
                play_audio("AUDIO/mantap.wav")
            elif int(self.timer_task_07) < 25:
                play_audio("AUDIO/kerja_bagus.wav")
            elif int(self.timer_task_07) < 30:
                play_audio("AUDIO/ayo_semangat.wav")
            else:
                play_audio("AUDIO/jangan_menyerah.wav")
            if self.max_level != 7:
                play_audio("AUDIO/lanjut_lvl8.wav")
            self.current_question = 8
            
        elif self.current_question == 8 and self.task_flag_08:
            self.timer_task_08 = time_elapsed
            self.timer_task_all.append(self.timer_task_08)
            self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_08))
            self.task_flag_08 = False
            print(f"TASK 8 SKIPPED after {self.timer_task_08} seconds")
            if int(self.timer_task_08) < 10:
                play_audio("AUDIO/menakjubkan.wav")
            elif int(self.timer_task_08) < 15:
                play_audio("AUDIO/hebat_sekali.wav")
            elif int(self.timer_task_08) < 20:
                play_audio("AUDIO/mantap.wav")
            elif int(self.timer_task_08) < 25:
                play_audio("AUDIO/kerja_bagus.wav")
            elif int(self.timer_task_08) < 30:
                play_audio("AUDIO/ayo_semangat.wav")
            else:
                play_audio("AUDIO/jangan_menyerah.wav")
            self.current_question = 9
            # No need to change current_question as this is the last level
        
        # Report completed level to API
        _done_level = len(self.timer_task_all)
        _done_time  = self.timer_task_all[-1] if self.timer_task_all else 0.0
        self._api_event("level_complete", level=_done_level, time_sec=_done_time)

        # Mark badge completed
        self._update_level_badge(_done_level, state="completed")

        # Update the display for the next level if not the last level
        if 1 <= self.current_question <= self.max_level:
            variant = self.get_random_variant(self.current_question)
            self.current_variant = variant

            # Load image with button mode support
            self.load_level_image(variant)
            self.current_level_button.grid_remove()
            self.show_current_level_button(self.current_question)
            # Reset the start time for the next level
            self.start_task = time.time()
            self._api_event("level_start", level=self.current_question)
            self.reset_timer()
            self.start_timer()
        elif self.current_question > self.max_level:
            self.end_test()
    
    def get_random_variant(self, level):
        """Get a variant for the specified level, using custom levels if available."""
        # If we have a custom level defined, use it
        if level in CUSTOM_LEVELS:
            return CUSTOM_LEVELS[level]
            
        # Otherwise, choose a random variant (a-d)
        variants = ['a', 'b', 'c', 'd']
        return f"{level}{random.choice(variants)}"

    def _show_countdown(self, count=3, on_done=None):
        """Show 3-2-1 countdown overlay, then call on_done."""
        if count > 0:
            self.countdown_overlay.place(relx=0.5, rely=0.5, anchor="center")
            self.countdown_overlay.configure(text=str(count))
            self.after(800, lambda: self._show_countdown(count - 1, on_done))
        else:
            self.countdown_overlay.configure(text="GO!")
            self.after(400, lambda: self.countdown_overlay.place_forget())
            if on_done:
                on_done()

    def _start_game(self):
        """Actual game start after countdown."""
        # Load and resize the initial image
        variant = self.get_random_variant(self.current_question)
        self.current_variant = variant
        
        # Use helper method to load image with button mode support
        self.load_level_image(variant)
        
        self._start_heartbeat()
        self.start_task = time.time()
        self._api_event("level_start", level=1)

        # Tournament cup: notify backend that match has started
        if TOURNAMENT_MODE and CURRENT_ROOM_CODE:
            _send_tournament_event(CURRENT_ROOM_CODE, "match_started")

        self.streaming()

        # Reset and start the timer
        self.reset_timer()
        self.start_timer()

    def button_0_callback(self):
        # Hide the start button
        self.button_0.grid_remove()
        # Show the current level button in the same position
        self.show_current_level_button(self.current_question)
        
        play_audio("AUDIO/hitung_mundur.wav")
        print("START")
        
        self._show_countdown(count=3, on_done=self._start_game)

    #def button_1_callback(self):

        #os.execl(sys.executable, os.path.abspath(__file__), *sys.argv)
        #sys.exit() 



    def estimate_cognitive_age(self, time_finish_one_task):
        
        from sklearn.preprocessing import PolynomialFeatures
        from sklearn.linear_model import LinearRegression

        # Transform the input features to higher-degree polynomials, Create a polynomial regression model
        # Fit the model to the transformed data, Predict y values for new x values

        degree = 2
        poly_features = PolynomialFeatures(degree=degree)

        #x_time      = np.array([13, 14, 15, 16, 17, 18, 22, 25, 30, 33, 37, 40, 45, 50]).reshape(-1, 1)
        
        x_time      = np.array([ 9, 10, 11, 12, 14, 16, 18, 20, 25, 30, 35, 40, 45, 50]).reshape(-1, 1)
        y_age       = np.array([20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85])

        time_avg = time_finish_one_task # * 1.5  # second

        x_time_poly = poly_features.fit_transform(x_time)
        model_time = LinearRegression()
        model_time.fit(x_time_poly, y_age)

        new_x_time = np.array([time_avg]).reshape(-1, 1)
        new_x_time_poly = poly_features.transform(new_x_time)
        cognitive_age = model_time.predict(new_x_time_poly)

        #print(cognitive_age)

        if (cognitive_age < 20):
            cognitive_age = 20
        elif (cognitive_age > 85):
            cognitive_age = 90
        
        cognitive_age = int(cognitive_age)

        return cognitive_age #.tolist()[0]

        #print("Estimated Cognitive Age : " + str(round(cognitive_age[0], 1)) + " years")

    def _update_level_badge(self, level, state="active"):
        """Update progress badges: completed = gold, active = red, upcoming = gray."""
        for i, badge in enumerate(self.level_badges):
            if i + 1 < level:
                badge.configure(fg_color=_CLR_SUCCESS, text_color=_CLR_TEXT)
            elif i + 1 == level:
                if state == "active":
                    badge.configure(fg_color=_CLR_ACCENT, text_color=_CLR_TEXT)
                else:
                    badge.configure(fg_color=_CLR_SUCCESS, text_color=_CLR_TEXT)
            else:
                badge.configure(fg_color=_CLR_CARD, text_color=_CLR_MUTED)

    def _update_stars(self, level):
        """Update star progress: completed = gold , active = orange , upcoming = gray """
        for i, star in enumerate(self.star_labels):
            if i + 1 < level:
                star.configure(text="", text_color=_CLR_SUCCESS)
            elif i + 1 == level:
                star.configure(text="", text_color=_CLR_ACCENT)
            else:
                star.configure(text="", text_color=_CLR_MUTED)
        self.level_text.configure(text=f"LEVEL {level} / {MAX_LEVEL}")

    def show_current_level_button(self, level):
        self.current_level_button = customtkinter.CTkButton(
            self.top_container,
            text=f"Level {level}",
            font=(_FONT_PRIMARY[0], 28),
            fg_color=_CLR_ACCENT,
            hover_color=_CLR_ACCENT2,
            text_color=_CLR_TEXT,
        )
        
        self.current_level_button.grid(row=1, column=0, columnspan=2, padx=20, pady=10, sticky="ew")
        if hasattr(self, 'status_label'):
            self.status_label.configure(text=f"MODE: {PC_MODE.upper()}  |  LEVEL {level}/{MAX_LEVEL}")
        
        self._update_level_badge(level, state="active")
        self._update_stars(level)
    
    def retry_test(self):
        self.destroy()

        global IS_MULTIPLAYER, CURRENT_ROOM_CODE, PLAYER_NUM, current_participant_uid, nick_name, gender_code, age_range_code, TOURNAMENT_MODE
        IS_MULTIPLAYER    = False
        CURRENT_ROOM_CODE = ''
        PLAYER_NUM         = 0
        current_participant_uid = ""
        nick_name = ""
        gender_code = ""
        age_range_code = 0
        TOURNAMENT_MODE = False

        app_input = App_Input()
        app_input.mainloop()
        if not nick_name:
            return

        if PC_MODE == "competition":
            IS_MULTIPLAYER = True
            app_room = App_Room()
            app_room.mainloop()
            if not app_room.room_ready:
                return
            CURRENT_ROOM_CODE = app_room.room_code
            PLAYER_NUM        = app_room.player_num

        app = TimeIn()
        app.after(0, lambda: app.state('zoomed'))
        app.mainloop()

    def update_timer(self):
        if self.timer_running:
            elapsed = time.time() - self.start_time
            minutes = int(elapsed // 60)
            seconds = int(elapsed % 60)
            self.timer_label.configure(text=f"{minutes:02d}:{seconds:02d}")
            # Danger bar: 0-40s teal, 40-60s orange, 60s+ red
            if elapsed < 40:
                self.danger_bar.configure(progress_color=_CLR_TEAL)
            elif elapsed < 60:
                self.danger_bar.configure(progress_color=_CLR_ACCENT)
            else:
                self.danger_bar.configure(progress_color=_CLR_DANGER)
            self.danger_bar.set(min(elapsed / 80.0, 1.0))
            self.after(200, self.update_timer)

    def start_timer(self):
        if not self.timer_running:
            self.start_time = time.time()
            self.timer_running = True
            self.update_timer()

    def stop_timer(self):
        self.timer_running = False

    def reset_timer(self):
        self.timer_label.configure(text="00:00")
        self.timer_running = False
        self.danger_bar.set(0.0)
        self.danger_bar.configure(progress_color=_CLR_TEAL)

    def load_level_image(self, variant):
        """Load level image with button mode support"""
        # Load cached image
        cached_img = self.get_cached_level_image(variant)
        if cached_img:
            self.image = customtkinter.CTkImage(
                light_image=cached_img,
                size=(self.frame_width, self.frame_height)
            )
        
        # Button mode: Show image for 5 seconds then hide
        if self.button_mode:
            self.image_label.configure(image=self.image)
            self.image_visible = True
            self.image_show_time = time.time()
            print(">>> Image displayed (will hide after 5 seconds)")
        else:
            # Normal mode: Always show image
            self.image_label.configure(image=self.image)
    
    def handle_button_mode(self):
        """Handle button mode image visibility logic"""
        if not self.button_mode:
            return
        
        # Check if image should be hidden after 5 seconds
        if self.image_visible and self.image_show_time:
            elapsed = time.time() - self.image_show_time
            if elapsed >= self.image_display_duration:
                # Hide image by showing blank
                blank_image = Image.new('RGB', (self.frame_width, self.frame_height), color='gray')
                self.image = customtkinter.CTkImage(
                    light_image=blank_image,
                    size=(self.frame_width, self.frame_height)
                )
                self.image_label.configure(image=self.image)
                self.image_visible = False
                self.image_show_time = None
                print(">>> Image hidden (press button to show again)")
        
        # Check for button press from ESP32
        if self.serial_thread:
            message = self.serial_thread.get_message()
            if message == "disable_image" and not self.image_visible:
                # Show image again for 5 seconds
                variant = self.current_variant
                cached_img = self.get_cached_level_image(variant)
                if cached_img:
                    self.image = customtkinter.CTkImage(
                        light_image=cached_img,
                        size=(self.frame_width, self.frame_height)
                    )
                self.image_label.configure(image=self.image)
                self.image_visible = True
                self.image_show_time = time.time()
                print(">>> Button pressed - Image displayed (will hide after 5 seconds)")
    
    # code for video streaming
    def streaming(self):

        self.button_0._state = "disabled"

        ret, frame = cap.read()
        if not ret:
            print("Failed to read frame from camera")
            self.after(100, self.streaming)
            return
            
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Apply camera settings
        if CAMERA_MIRROR_X:
            frame = cv2.flip(frame, 1)
        if CAMERA_MIRROR_Y:
            frame = cv2.flip(frame, 0)
        if CAMERA_ZOOM != 1.0:
            h, w = frame.shape[:2]
            new_w, new_h = int(w * CAMERA_ZOOM), int(h * CAMERA_ZOOM)
            frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            if CAMERA_ZOOM > 1.0:
                start_x = (new_w - w) // 2
                start_y = (new_h - h) // 2
                frame = frame[start_y:start_y+h, start_x:start_x+w]

        # Handle button mode image visibility
        self.handle_button_mode()
        # Don't resize to 1000x800, use original resolution for better performance
        # frame = cv2.resize(frame, (1000, 800))

        # --------------------------------------------------------------------------- Start Process

        # Frame threshold 
        imgBlur = cv2.GaussianBlur(frame, (7,7), 1)
        imgGray = cv2.cvtColor(imgBlur, cv2.COLOR_RGB2GRAY)
        ret, imgThres = cv2.threshold(imgGray, 175, 255, cv2.THRESH_BINARY) #175   195
        
        # Make detections using background thread (non-blocking)
        self.frame_count += 1
        
        # Only submit frame to YOLO every N frames to reduce load
        if self.frame_count % (self.yolo_skip_frames + 1) == 0:
            if self.yolo_thread.frame_queue.empty():  # Only if queue is empty
                try:
                    # MUST copy frame to avoid race condition with drawing operations
                    # Use numpy copy which is faster than frame.copy()
                    frame_for_yolo = np.array(frame, copy=True)
                    self.yolo_thread.frame_queue.put_nowait(frame_for_yolo)
                except:
                    pass  # Queue full, skip this frame
        
        # Get latest detection results if available (non-blocking)
        if not self.yolo_thread.result_queue.empty():
            try:
                self.latest_detections = self.yolo_thread.result_queue.get_nowait()
            except:
                pass
        
        # Use latest detections (may be from previous frame, but that's OK)
        list_tracked_objects = self.latest_detections

        #if len(list_tracked_objects) == 4: #>0
        
        box_num = 0
        box_face = 0
        box_list = []
        box_design = []
        box_distance = []
        box_design_sort = []

        box_near_hand = []
        #avg_confidence = []

        pos_x = []
        pos_y = []

        for x1, y1, x2, y2, conf_pred, cls_id, cls in list_tracked_objects:

            if conf_pred > 0.7:

                #avg_confidence.append( round(conf_pred,2) )

                center_x = int ((x1+x2)/2)
                center_y = int ((y1+y2)/2)
                x1 = int(x1)
                x2 = int(x2)
                y1 = int(y1)
                y2 = int(y2)
                w = int (x2-x1)
                h = int (y2-y1)

                box_distance.append( int (math.sqrt( pow(center_x, 2) + pow(center_y, 2) )) )
                #print(center_x, center_y)

                pos_x.append( int (center_x) )
                pos_y.append( int (center_y) )
                
                # Optimize: check bounds before resize to avoid errors
                if y1 >= 0 and y2 <= imgThres.shape[0] and x1 >= 0 and x2 <= imgThres.shape[1] and (y2-y1) > 0 and (x2-x1) > 0:
                    dim = (100, 100)
                    imgBox = cv2.resize(imgThres[y1:y2, x1:x2], dim, interpolation = cv2.INTER_AREA)
                else:
                    # Skip invalid box
                    continue
                #cv2.imshow("Box_"+str(box_num), imgBox)
                
                box_class = [ imgBox[50,25], imgBox[75,50], imgBox[50,75], imgBox[25,50] ]

                if box_class == [0,0,0,0] :
                    box_face = 1
                    box_design.append(1)
                elif box_class == [255,255,255,255]:
                    box_face = 2
                    box_design.append(2)
                #... dipisah
                elif box_class == [255,255,0,0]:
                    box_face = 3
                    box_design.append(3)  
                elif box_class == [255,0,0,255]:
                    box_face = 4
                    box_design.append(4)  
                elif box_class == [0,0,255,255]:
                    box_face = 5
                    box_design.append(5)  
                elif box_class == [0,255,255,0]:
                    box_face = 6
                    box_design.append(6)               
                
                cv2.rectangle(frame, (x1,y1), (x1+w, y1+h), (0, 255, 0), 2)

                box_num = box_num + 1
                roi_label = frame[y1:y1+50, x1:x1+50]

                if(box_face == 1):
                    try:                            
                        roi_label [np.where(mask_face_01)] = 0
                        roi_label += face_01
                    except IndexError:
                        pass
                elif(box_face == 2):
                    try:                            
                        roi_label [np.where(mask_face_02)] = 0
                        roi_label += face_02
                    except IndexError:
                        pass
                elif(box_face == 3):
                    try:                            
                        roi_label [np.where(mask_face_03)] = 0
                        roi_label += face_03
                    except IndexError:
                        pass
                elif(box_face == 4):
                    try:                            
                        roi_label [np.where(mask_face_04)] = 0
                        roi_label += face_04
                    except IndexError:
                        pass
                elif(box_face == 5):
                    try:                            
                        roi_label [np.where(mask_face_05)] = 0
                        roi_label += face_05
                    except IndexError:
                        pass
                elif(box_face == 6):
                    try:                            
                        roi_label [np.where(mask_face_06)] = 0
                        roi_label += face_06
                    except IndexError:
                        pass

        
        # >>>>>>>>>>>>

        if len(box_design) == 4 and len(box_distance) == 4:

            box_0 = (pos_x[0], pos_y[0])
            box_1 = (pos_x[1], pos_y[1])
            box_2 = (pos_x[2], pos_y[2])
            box_3 = (pos_x[3], pos_y[3])

            # Draw lines connecting boxes (optimized with polylines)
            pts = np.array([
                [pos_x[0], pos_y[0]],
                [pos_x[1], pos_y[1]],
                [pos_x[2], pos_y[2]],
                [pos_x[3], pos_y[3]]
            ], np.int32)
            # Draw all connecting lines at once
            for i in range(4):
                for j in range(i+1, 4):
                    cv2.line(frame, tuple(pts[i]), tuple(pts[j]), (0, 0, 0), 2)

            #pos_x_order = [ pos_x[0], pos_x[1], pos_x[2], pos_x[3] ]
            #pos_y_order = [ pos_y[0], pos_y[1], pos_y[2], pos_y[3] ]

            len_0 = int (math.sqrt( (pos_x[0]-pos_x[1])**2 + (pos_y[0]-pos_y[1])**2 ) )
            len_1 = int (math.sqrt( (pos_x[1]-pos_x[2])**2 + (pos_y[1]-pos_y[2])**2 ) )
            len_2 = int (math.sqrt( (pos_x[2]-pos_x[3])**2 + (pos_y[2]-pos_y[3])**2 ) )
            len_3 = int (math.sqrt( (pos_x[3]-pos_x[0])**2 + (pos_y[3]-pos_y[0])**2 ) )
            len_4 = int (math.sqrt( (pos_x[0]-pos_x[2])**2 + (pos_y[0]-pos_y[2])**2 ) )
            len_5 = int (math.sqrt( (pos_x[1]-pos_x[3])**2 + (pos_y[1]-pos_y[3])**2 ) )

            # Order Len
            len_order = [ len_0, len_1, len_2, len_3, len_4, len_5 ]
            len_rect = sorted(len_order)

            #print(len_rect)

            if  ( abs(len_rect[0] - len_rect[1]) < 100) and \
                ( abs(len_rect[0] - len_rect[2]) < 100) and \
                ( abs(len_rect[0] - len_rect[3]) < 100) and \
                ( abs(len_rect[1] - len_rect[2]) < 100) and \
                ( abs(len_rect[1] - len_rect[3]) < 100) and \
                ( abs(len_rect[2] - len_rect[3]) < 100):

                # 1. Gabungkan posisi x, y, dan index asli
                indexed_positions = [(pos_x[i], pos_y[i], i) for i in range(len(pos_x))]

                # 2. Urutkan berdasarkan x untuk memisahkan kelompok kiri dan kanan
                sorted_by_x = sorted(pos_x)
                mid_point = (sorted_by_x[1] + sorted_by_x[2]) / 2  # Titik tengah antara x terbesar kedua dan terkecil kedua

                # 3. Urutkan: kelompok kiri (x < mid_point) dulu, lalu kelompok kanan, dalam setiap kelompok urutkan berdasarkan y
                indexed_positions.sort(key=lambda p: (p[0] >= mid_point, p[1]))

                # 4. Ambil index asli yang sudah terurut
                sort_index = [idx for (x, y, idx) in indexed_positions]

                # 5. Urutkan box_design berdasarkan index yang sudah diurutkan
                box_design_sort = [box_design[i] for i in sort_index]

                current_answer = LEVEL_ANSWERS.get(self.current_variant, [])    
                if self.current_question == 1 and box_design_sort == current_answer:
                    test_label = frame[20:120, 20:120]
                    test_label [np.where(mask_img_01)] = 0
                    test_label += img_01
                    
                    #self.task_state = self.task_state + 1
                    
                    if(self.task_flag_01):
                        end_task = time.time()
                        self.timer_task_01 = round((end_task - self.start_task - timer_return), 2)
                        self.timer_task_all.append(self.timer_task_01)

                        self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_01))

                        print ("TASK 1 COMPLETED in " + str(self.timer_task_01) +" seconds")
                        self._api_event("level_complete", level=1, time_sec=self.timer_task_01)

                        if int(self.timer_task_01) < 10:
                            play_audio("AUDIO/menakjubkan.wav")
                        elif int(self.timer_task_01) < 15:
                            play_audio("AUDIO/hebat_sekali.wav")
                        elif int(self.timer_task_01) < 20:
                            play_audio("AUDIO/mantap.wav")
                        elif int(self.timer_task_01) < 25:
                            play_audio("AUDIO/kerja_bagus.wav")
                        elif int(self.timer_task_01) < 30:
                            play_audio("AUDIO/ayo_semangat.wav")
                        else:
                            play_audio("AUDIO/jangan_menyerah.wav")

                        if self.max_level == 1:
                            self.end_test()
                        else:
                            play_audio("AUDIO/lanjut_lvl2.wav")

                            self.current_question = 2
                            variant = self.get_random_variant(self.current_question)
                            self.current_variant = variant
                            current_answer = LEVEL_ANSWERS.get(self.current_variant, [])
            
                            # Load image with button mode support
                            self.load_level_image(variant)
                            self.current_level_button.grid_remove()
                            self.show_current_level_button(self.current_question)
                            self.task_flag_01 = False
                            self.start_task = time.time()
                            self._api_event("level_start", level=2)
                            # Reset and start the timer
                            self.reset_timer()
                            self.start_timer()

                elif self.current_question == 2 and box_design_sort == current_answer:
                    test_label = frame[20:120, 20:120]
                    test_label [np.where(mask_img_02)] = 0
                    test_label += img_02
                    
                    #self.task_state = self.task_state + 1
                    
                    if(self.task_flag_02):
                        end_task = time.time()
                        self.timer_task_02 = round((end_task - self.start_task - timer_return), 2)
                        self.timer_task_all.append(self.timer_task_02)

                        self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_02))

                        print ("TASK 2 COMPLETED in " + str(self.timer_task_02) +" seconds")
                        self._api_event("level_complete", level=2, time_sec=self.timer_task_02)

                        if int(self.timer_task_02) < 10:
                            play_audio("AUDIO/menakjubkan.wav")
                        elif int(self.timer_task_02) < 15:
                            play_audio("AUDIO/hebat_sekali.wav")
                        elif int(self.timer_task_02) < 20:
                            play_audio("AUDIO/mantap.wav")
                        elif int(self.timer_task_02) < 25:
                            play_audio("AUDIO/kerja_bagus.wav")
                        elif int(self.timer_task_02) < 30:
                            play_audio("AUDIO/ayo_semangat.wav")
                        else:
                            play_audio("AUDIO/jangan_menyerah.wav")

                        if self.max_level == 2:
                            self.end_test()
                        else:
                            play_audio("AUDIO/lanjut_lvl3.wav")

                            self.current_question = 3
                            variant = self.get_random_variant(self.current_question)
                            self.current_variant = variant
                            current_answer = LEVEL_ANSWERS.get(self.current_variant, [])
            
                            # Load image with button mode support
                            self.load_level_image(variant)
                            self.current_level_button.grid_remove()
                            self.show_current_level_button(self.current_question)
                            self.task_flag_02 = False
                            self.start_task = time.time()
                            self._api_event("level_start", level=3)
                            # Reset and start the timer
                            self.reset_timer()
                            self.start_timer()

                elif self.current_question == 3 and box_design_sort == current_answer:
                    test_label = frame[20:120, 20:120]
                    test_label [np.where(mask_img_03)] = 0
                    test_label += img_03
                    
                    #self.task_state = self.task_state + 1
                    
                    if(self.task_flag_03):
                        end_task = time.time()
                        self.timer_task_03 = round((end_task - self.start_task - timer_return), 2)
                        self.timer_task_all.append(self.timer_task_03)

                        self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_03))

                        print ("TASK 3 COMPLETED in " + str(self.timer_task_03) +" seconds")
                        self._api_event("level_complete", level=3, time_sec=self.timer_task_03)

                        if int(self.timer_task_03) < 10:
                            play_audio("AUDIO/menakjubkan.wav")
                        elif int(self.timer_task_03) < 15:
                            play_audio("AUDIO/hebat_sekali.wav")
                        elif int(self.timer_task_03) < 20:
                            play_audio("AUDIO/mantap.wav")
                        elif int(self.timer_task_03) < 25:
                            play_audio("AUDIO/kerja_bagus.wav")
                        elif int(self.timer_task_03) < 30:
                            play_audio("AUDIO/ayo_semangat.wav")
                        else:
                            play_audio("AUDIO/jangan_menyerah.wav")

                        if self.max_level == 3:
                            self.end_test()
                        else:
                            play_audio("AUDIO/lanjut_lvl4.wav")

                            self.current_question = 4
                            variant = self.get_random_variant(self.current_question)
                            self.current_variant = variant
                            current_answer = LEVEL_ANSWERS.get(self.current_variant, [])
            
                            # Load image with button mode support
                            self.load_level_image(variant)
                            self.current_level_button.grid_remove()
                            self.show_current_level_button(self.current_question)
                            self.task_flag_03 = False
                            self.start_task = time.time()
                            self._api_event("level_start", level=4)
                            # Reset and start the timer
                            self.reset_timer()
                            self.start_timer()

                elif self.current_question == 4 and box_design_sort == current_answer:
                    test_label = frame[20:120, 20:120]
                    test_label [np.where(mask_img_04)] = 0
                    test_label += img_04
                    
                    #self.task_state = self.task_state + 1
                    
                    if(self.task_flag_04):
                        end_task = time.time()
                        self.timer_task_04 = round((end_task - self.start_task - timer_return), 2)
                        self.timer_task_all.append(self.timer_task_04)

                        self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_04))

                        print ("TASK 4 COMPLETED in " + str(self.timer_task_04) +" seconds")
                        self._api_event("level_complete", level=4, time_sec=self.timer_task_04)

                        if int(self.timer_task_04) < 10:
                            play_audio("AUDIO/menakjubkan.wav")
                        elif int(self.timer_task_04) < 15:
                            play_audio("AUDIO/hebat_sekali.wav")
                        elif int(self.timer_task_04) < 20:
                            play_audio("AUDIO/mantap.wav")
                        elif int(self.timer_task_04) < 25:
                            play_audio("AUDIO/kerja_bagus.wav")
                        elif int(self.timer_task_04) < 30:
                            play_audio("AUDIO/ayo_semangat.wav")
                        else:
                            play_audio("AUDIO/jangan_menyerah.wav")

                        if self.max_level == 4:
                            self.end_test()
                        else:
                            play_audio("AUDIO/lanjut_lvl5.wav")

                            self.current_question = 5
                            variant = self.get_random_variant(self.current_question)
                            self.current_variant = variant
                            current_answer = LEVEL_ANSWERS.get(self.current_variant, [])
            
                            # Load image with button mode support
                            self.load_level_image(variant)
                            self.current_level_button.grid_remove()
                            self.show_current_level_button(self.current_question)
                            self.task_flag_04 = False
                            self.start_task = time.time()
                            self._api_event("level_start", level=5)
                            # Reset and start the timer
                            self.reset_timer()
                            self.start_timer()


                elif self.current_question == 5 and box_design_sort == current_answer:
                    test_label = frame[20:120, 20:120]
                    test_label [np.where(mask_img_05)] = 0
                    test_label += img_05

                    #self.task_state = self.task_state + 1
                    
                    if(self.task_flag_05):
                        end_task = time.time()
                        self.timer_task_05 = round((end_task - self.start_task - timer_return), 2)
                        self.timer_task_all.append(self.timer_task_05)

                        self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_05))

                        print ("TASK 5 COMPLETED in " + str(self.timer_task_05) +" seconds")
                        self._api_event("level_complete", level=5, time_sec=self.timer_task_05)

                        if int(self.timer_task_05) < 10:
                            play_audio("AUDIO/menakjubkan.wav")
                        elif int(self.timer_task_05) < 15:
                            play_audio("AUDIO/hebat_sekali.wav")
                        elif int(self.timer_task_05) < 20:
                            play_audio("AUDIO/mantap.wav")
                        elif int(self.timer_task_05) < 25:
                            play_audio("AUDIO/kerja_bagus.wav")
                        elif int(self.timer_task_05) < 30:
                            play_audio("AUDIO/ayo_semangat.wav")
                        else:
                            play_audio("AUDIO/jangan_menyerah.wav")

                        if self.max_level == 5:
                            self.end_test()
                        else:
                            play_audio("AUDIO/lanjut_lvl6.wav")

                            self.current_question = 6
                            variant = self.get_random_variant(self.current_question)
                            self.current_variant = variant
                            current_answer = LEVEL_ANSWERS.get(self.current_variant, [])
            
                            # Load image with button mode support
                            self.load_level_image(variant)
                            self.current_level_button.grid_remove()
                            self.show_current_level_button(self.current_question)
                            self.task_flag_05 = False
                            self.start_task = time.time()
                            self._api_event("level_start", level=6)
                            # Reset and start the timer
                            self.reset_timer()
                            self.start_timer()
                    

                elif self.current_question == 6 and box_design_sort == current_answer:
                    test_label = frame[20:120, 20:120]
                    test_label [np.where(mask_img_06)] = 0
                    test_label += img_06

                    #self.task_state = self.task_state + 1
                    
                    if(self.task_flag_06):
                        end_task = time.time()
                        self.timer_task_06 = round((end_task - self.start_task - timer_return), 2)
                        self.timer_task_all.append(self.timer_task_06)

                        self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_06))

                        print ("TASK 6 COMPLETED in " + str(self.timer_task_06) +" seconds")
                        self._api_event("level_complete", level=6, time_sec=self.timer_task_06)

                        if int(self.timer_task_06) < 10:
                            play_audio("AUDIO/menakjubkan.wav")
                        elif int(self.timer_task_06) < 15:
                            play_audio("AUDIO/hebat_sekali.wav")
                        elif int(self.timer_task_06) < 20:
                            play_audio("AUDIO/mantap.wav")
                        elif int(self.timer_task_06) < 25:
                            play_audio("AUDIO/kerja_bagus.wav")
                        elif int(self.timer_task_06) < 30:
                            play_audio("AUDIO/ayo_semangat.wav")
                        else:
                            play_audio("AUDIO/jangan_menyerah.wav")

                        if self.max_level == 6:
                            self.end_test()
                        else:
                            play_audio("AUDIO/lanjut_lvl7.wav")

                            self.current_question = 7
                            variant = self.get_random_variant(self.current_question)
                            self.current_variant = variant
                            current_answer = LEVEL_ANSWERS.get(self.current_variant, [])
            
                            # Load image with button mode support
                            self.load_level_image(variant)
                            self.current_level_button.grid_remove()
                            self.show_current_level_button(self.current_question)
                            self.task_flag_06 = False
                            self.start_task = time.time()
                            self._api_event("level_start", level=7)
                            # Reset and start the timer
                            self.reset_timer()
                            self.start_timer()

                elif self.current_question == 7 and box_design_sort == current_answer:
                    test_label = frame[20:120, 20:120]
                    test_label [np.where(mask_img_07)] = 0
                    test_label += img_07
                    
                    #self.task_state = self.task_state + 1
                    
                    if(self.task_flag_07):
                        end_task = time.time()
                        self.timer_task_07 = round((end_task - self.start_task - timer_return), 2)
                        self.timer_task_all.append(self.timer_task_07)

                        self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_07))

                        print ("TASK 7 COMPLETED in " + str(self.timer_task_07) +" seconds")
                        self._api_event("level_complete", level=7, time_sec=self.timer_task_07)

                        if int(self.timer_task_07) < 10:
                            play_audio("AUDIO/menakjubkan.wav")
                        elif int(self.timer_task_07) < 15:
                            play_audio("AUDIO/hebat_sekali.wav")
                        elif int(self.timer_task_07) < 20:
                            play_audio("AUDIO/mantap.wav")
                        elif int(self.timer_task_07) < 25:
                            play_audio("AUDIO/kerja_bagus.wav")
                        elif int(self.timer_task_07) < 30:
                            play_audio("AUDIO/ayo_semangat.wav")
                        else:
                            play_audio("AUDIO/jangan_menyerah.wav")

                        if self.max_level == 7:
                            self.end_test()
                        else:
                            play_audio("AUDIO/lanjut_lvl8.wav")

                            self.current_question = 8
                            variant = self.get_random_variant(self.current_question)
                            self.current_variant = variant
                            current_answer = LEVEL_ANSWERS.get(self.current_variant, [])
            
                            # Load image with button mode support
                            self.load_level_image(variant)
                            self.current_level_button.grid_remove()
                            self.show_current_level_button(self.current_question)
                            self.task_flag_07 = False
                            self.start_task = time.time()
                            self._api_event("level_start", level=8)
                            # Reset and start the timer
                            self.reset_timer()
                            self.start_timer()

                elif self.current_question == 8 and box_design_sort == current_answer:
                    test_label = frame[20:120, 20:120]
                    test_label [np.where(mask_img_08)] = 0
                    test_label += img_08

                    #self.task_state = self.task_state + 1
                    
                    if(self.task_flag_08):
                        end_task = time.time()
                        self.timer_task_08 = round((end_task - self.start_task - timer_return), 2)
                        self.timer_task_all.append(self.timer_task_08)

                        self.cognitive_age_list.append(self.estimate_cognitive_age(self.timer_task_08))

                        print ("TASK 8 COMPLETED in " + str(self.timer_task_08) +" seconds")
                        self._api_event("level_complete", level=8, time_sec=self.timer_task_08)

                        if int(self.timer_task_08) < 10:
                            play_audio("AUDIO/menakjubkan.wav")
                        elif int(self.timer_task_08) < 15:
                            play_audio("AUDIO/hebat_sekali.wav")
                        elif int(self.timer_task_08) < 20:
                            play_audio("AUDIO/mantap.wav")
                        elif int(self.timer_task_08) < 25:
                            play_audio("AUDIO/kerja_bagus.wav")
                        elif int(self.timer_task_08) < 30:
                            play_audio("AUDIO/ayo_semangat.wav")
                        else:
                            play_audio("AUDIO/jangan_menyerah.wav")

                        self.task_flag_08 = False
                        self.end_test()
                else:
                    #print ("NOT COMPLETE")
                    pass

                box_design = []
                box_distance = []
                box_design_sort = []
    


        # --------------------------------------------------------------------------- End Process

        # Use persistent MediaPipe Hands instance (not recreated every frame)
        # Skip MediaPipe on same frames as YOLO to reduce load
        if self.mp_hands_detector is not None and self.frame_count % (self.mediapipe_skip_frames + 1) == 0:
            # frame_rgb already in RGB format, no need to convert
            hand_result = self.mp_hands_detector.process(frame)

            if hand_result and hand_result.multi_hand_landmarks:
                for hand_landmarks in hand_result.multi_hand_landmarks:
                    mp_drawing.draw_landmarks(frame, hand_landmarks, mp_hands.HAND_CONNECTIONS, landmark_style, connection_style)

        # Update camera display only if camera is not hidden
        if not HIDE_CAMERA and self.camera is not None:
            # Convert frame to ImageTk format
            # img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            img = Image.fromarray(frame)
            
            # Calculate aspect ratio
            frame_height, frame_width = frame.shape[:2]
            container_width = self.video_frame_1.winfo_width()
            container_height = self.video_frame_1.winfo_height()
            
            # Calculate scaling factor while maintaining aspect ratio
            width_ratio = container_width / frame_width
            height_ratio = container_height / frame_height
            scale = min(width_ratio, height_ratio)
            
            # Resize image if needed
            if scale < 1:
                new_width = int(frame_width * scale)
                new_height = int(frame_height * scale)
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            ImgTks = ImageTk.PhotoImage(image=img)
            self._last_photo_ref = ImgTks
            self.camera.configure(image=ImgTks)
        
        # FPS monitoring
        self.fps_counter += 1
        if time.time() - self.fps_start_time >= 1.0:
            self.current_fps = self.fps_counter
            
            # Enhanced FPS output with debug info
            if self.debug_mode:
                queue_size = self.yolo_thread.frame_queue.qsize()
                result_size = self.yolo_thread.result_queue.qsize()
                last_inference = self.yolo_thread.last_inference_time
                print(f"FPS: {self.current_fps} | YOLO: {last_inference:.1f}ms | Queue: {queue_size}/{result_size}")
            else:
                print(f"FPS: {self.current_fps}")
            
            self.fps_counter = 0
            self.fps_start_time = time.time()

        # Reduce delay for better responsiveness
        self.after(10, self.streaming)
    
    def cleanup(self):
        """Cleanup resources before closing"""
        print(">>> Cleaning up resources...")
        if hasattr(self, 'yolo_thread'):
            self.yolo_thread.stop()
        if hasattr(self, 'serial_thread') and self.serial_thread:
            self.serial_thread.stop()
        if hasattr(self, 'mp_hands_detector') and self.mp_hands_detector is not None:
            self.mp_hands_detector.close()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    
    def destroy(self):
        """Override destroy to cleanup resources"""
        self.cleanup()
        super().destroy()

    def _open_camera_settings(self):
        """Open camera settings dialog"""
        dialog = CameraSettingsDialog(self)

    def _update_camera_settings(self, mirror_x, mirror_y, zoom):
        """Apply camera settings and save to .env"""
        global CAMERA_MIRROR_X, CAMERA_MIRROR_Y, CAMERA_ZOOM
        CAMERA_MIRROR_X = mirror_x
        CAMERA_MIRROR_Y = mirror_y
        CAMERA_ZOOM = zoom

        # Save to .env
        env_path = Path('.') / '.env'
        lines = []
        if env_path.exists():
            with open(env_path, 'r') as f:
                lines = f.readlines()

        updates = {
            'CAMERA_MIRROR_X': str(mirror_x).lower(),
            'CAMERA_MIRROR_Y': str(mirror_y).lower(),
            'CAMERA_ZOOM': str(zoom),
        }

        updated_lines = []
        for line in lines:
            stripped = line.strip()
            prefix = stripped.split('=')[0] if '=' in stripped else ''
            if prefix in updates:
                updated_lines.append(f"{prefix}={updates[prefix]}\n")
            else:
                updated_lines.append(line)

        existing_prefixes = {l.split('=')[0] for l in updated_lines if '=' in l}
        for key, val in updates.items():
            if key not in existing_prefixes:
                updated_lines.append(f"{key}={val}\n")

        with open(env_path, 'w') as f:
            f.writelines(updated_lines)


class CameraSettingsDialog(customtkinter.CTkToplevel):
    """Camera settings dialog with live preview, mirror X/Y and zoom controls"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Pengaturan Kamera")
        self.configure(fg_color=_CLR_BG)
        self.resizable(0, 0)

        # Center over parent window
        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width() // 2) - 180
        py = parent.winfo_y() + (parent.winfo_height() // 2) - 200
        self.geometry(f"380x440+{px}+{py}")

        self.mirror_x = CAMERA_MIRROR_X
        self.mirror_y = CAMERA_MIRROR_Y
        self.zoom = CAMERA_ZOOM
        self.running = True

        self.transient(parent)
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)

        # ── Camera Preview ──────────────────────────────────────────────
        preview_card = customtkinter.CTkFrame(self, fg_color=_CLR_CARD, corner_radius=_CORNER_RADIUS, border_width=1, border_color=_CLR_SUBTLE_BORDER)
        preview_card.grid(row=0, column=0, padx=24, pady=(20, 10), sticky="ew")
        preview_card.grid_columnconfigure(0, weight=1)
        self.preview_label = customtkinter.CTkLabel(preview_card, text="", corner_radius=_CORNER_RADIUS_SMALL)
        self.preview_label.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self.preview_label.configure(height=240)

        # Start preview thread
        self._preview_thread = threading.Thread(target=self._preview_loop, daemon=True)
        self._preview_thread.start()

        # ── Controls ────────────────────────────────────────────────────
        body = customtkinter.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, padx=24, pady=(0, 10), sticky="ew")
        body.grid_columnconfigure(0, weight=1)

        # Mirror X
        self.mirror_x_var = customtkinter.BooleanVar(value=self.mirror_x)
        customtkinter.CTkCheckBox(
            body, text="Cermin Horizontal (kiri-kanan)",
            variable=self.mirror_x_var, font=(_FONT_PRIMARY[0], 13),
            fg_color=_CLR_ACCENT, hover_color=_CLR_ACCENT2,
        ).grid(row=0, column=0, sticky="w", pady=(0, 12))

        # Mirror Y
        self.mirror_y_var = customtkinter.BooleanVar(value=self.mirror_y)
        customtkinter.CTkCheckBox(
            body, text="Cermin Vertikal (atas-bawah)",
            variable=self.mirror_y_var, font=(_FONT_PRIMARY[0], 13),
            fg_color=_CLR_ACCENT, hover_color=_CLR_ACCENT2,
        ).grid(row=1, column=0, sticky="w", pady=(0, 12))

        # Zoom slider
        customtkinter.CTkLabel(
            body, text="Zoom",
            font=(_FONT_PRIMARY[0], 13, "bold"), text_color=_CLR_MUTED,
        ).grid(row=2, column=0, sticky="w", pady=(0, 6))

        zoom_frame = customtkinter.CTkFrame(body, fg_color="transparent")
        zoom_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        zoom_frame.grid_columnconfigure(0, weight=1)

        self.zoom_slider = customtkinter.CTkSlider(
            zoom_frame, from_=0.5, to=3.0, number_of_steps=25,
            command=self._on_zoom_change
        )
        self.zoom_slider.set(self.zoom)
        self.zoom_slider.grid(row=0, column=1, sticky="ew")

        customtkinter.CTkLabel(
            zoom_frame, text="0.5x", font=(_FONT_PRIMARY[0], 10),
            text_color=_CLR_MUTED,
        ).grid(row=1, column=0, sticky="w")

        self.zoom_label = customtkinter.CTkLabel(
            zoom_frame, text=f"{self.zoom:.1f}x",
            font=(_FONT_PRIMARY[0], 14, "bold"), text_color=_CLR_ACCENT, width=50
        )
        self.zoom_label.grid(row=0, column=2, padx=(10, 0))

        customtkinter.CTkLabel(
            zoom_frame, text="3.0x", font=(_FONT_PRIMARY[0], 10),
            text_color=_CLR_MUTED,
        ).grid(row=1, column=2, sticky="e")

        # Zoom preset buttons
        preset_frame = customtkinter.CTkFrame(body, fg_color="transparent")
        preset_frame.grid(row=4, column=0, sticky="ew", pady=(0, 16))
        for i, z in enumerate([1.0, 1.5, 2.0]):
            preset_frame.grid_columnconfigure(i, weight=1)
            customtkinter.CTkButton(
                preset_frame, text=f"{z:.1f}x",
                command=lambda v=z: self._set_zoom(v),
                font=(_FONT_PRIMARY[0], 12, "bold"),
                fg_color=_CLR_CARD, hover_color=_CLR_SUBTLE_BORDER,
                text_color=_CLR_TEXT, corner_radius=_CORNER_RADIUS_SMALL, height=36,
            ).grid(row=0, column=i, padx=(0 if i == 0 else 4, 4 if i < 2 else 0), sticky="ew")

        # Buttons
        btn_frame = customtkinter.CTkFrame(body, fg_color="transparent")
        btn_frame.grid(row=5, column=0, sticky="ew")
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)

        customtkinter.CTkButton(
            btn_frame, text="Batal",
            command=self._close,
            font=(_FONT_PRIMARY[0], 13, "bold"),
            fg_color=_CLR_SUBTLE_BORDER, hover_color=_CLR_ACCENT,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS, height=48,
        ).grid(row=0, column=0, padx=(0, 8), sticky="ew")

        customtkinter.CTkButton(
            btn_frame, text="Simpan",
            command=self._save,
            font=(_FONT_PRIMARY[0], 13, "bold"),
            fg_color=_CLR_ACCENT, hover_color=_CLR_ACCENT2,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS, height=48,
        ).grid(row=0, column=1, sticky="ew")

        self.protocol("WM_DELETE_WINDOW", self._close)

    def _preview_loop(self):
        """Read camera frames and update preview in main thread."""
        import cv2
        pw, ph = 320, 240
        self._current_imgtk = None
        while self.running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            # Apply current settings
            if self.mirror_x_var.get():
                frame = cv2.flip(frame, 1)
            if self.mirror_y_var.get():
                frame = cv2.flip(frame, 0)
            zoom = self.zoom_slider.get()
            if zoom != 1.0:
                h, w = frame.shape[:2]
                new_w, new_h = int(w * zoom), int(h * zoom)
                frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                if zoom > 1.0:
                    start_x = (new_w - w) // 2
                    start_y = (new_h - h) // 2
                    frame = frame[start_y:start_y+h, start_x:start_x+w]

            img = Image.fromarray(frame).resize((pw, ph), Image.LANCZOS)
            self._current_imgtk = ImageTk.PhotoImage(img)

            def upd(im=None):
                if im:
                    self.preview_label.configure(image=im)
            self.after(0, lambda im=self._current_imgtk: upd(im))
            time.sleep(0.03)

    def _on_zoom_change(self, value):
        self.zoom_label.configure(text=f"{value:.1f}x")

    def _set_zoom(self, value):
        self.zoom_slider.set(value)
        self._on_zoom_change(value)

    def _save(self):
        mirror_x = self.mirror_x_var.get()
        mirror_y = self.mirror_y_var.get()
        zoom = round(self.zoom_slider.get(), 1)
        self.master._update_camera_settings(mirror_x, mirror_y, zoom)
        self._close()

    def _close(self):
        self.running = False
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  App_Room  — Multiplayer lobby (create / join / ready)
# ─────────────────────────────────────────────────────────────────────────────

class App_Room(customtkinter.CTk):
    def __init__(self):
        super().__init__()
        self.title("BDT — Multiplayer Lobby")
        self.geometry("620x600")
        self.configure(fg_color=_CLR_BG)
        self.resizable(0, 0)
        # Center on screen
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (w // 2)
        y = (self.winfo_screenheight() // 2) - (h // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

        self.room_code  = None
        self.player_num = None
        self.room_ready = False
        self._polling   = False
        self._i_am_ready = False

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        self._refresh_rooms()

    def report_callback_exception(self, exc, val, tb):
        log_exception("Tk callback exception (App_Room)", exc, val, tb)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # Header with refined accent bottom border
        header = customtkinter.CTkFrame(self, fg_color=_CLR_CARD, corner_radius=0, height=68, border_width=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(0, weight=1)
        header.grid_columnconfigure(1, weight=0)
        customtkinter.CTkLabel(
            header, text="Multiplayer Lobby",
            font=(_FONT_PRIMARY[0], 24, "bold"), text_color=_CLR_TEXT,
        ).grid(row=0, column=0, padx=24, pady=18, sticky="w")
        self.conn_pill = customtkinter.CTkLabel(
            header, text="ONLINE",
            font=(_FONT_PRIMARY[0], 10, "bold"), text_color=_CLR_TEXT,
            fg_color=_CLR_SUCCESS, corner_radius=_CORNER_RADIUS_PILL, width=64, height=24,
        )
        self.conn_pill.grid(row=0, column=1, padx=(0, 24), pady=18, sticky="e")

        # Refined accent line below header
        line = customtkinter.CTkFrame(self, fg_color=_CLR_ACCENT, height=3, corner_radius=0)
        line.grid(row=1, column=0, sticky="ew")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ── Find-Room panel ──
        self.find_frame = customtkinter.CTkFrame(self, fg_color=_CLR_BG)
        self.find_frame.grid(row=2, column=0, sticky="nsew", padx=24, pady=20)
        self.find_frame.grid_columnconfigure(0, weight=1)

        customtkinter.CTkButton(
            self.find_frame, text="Buat Room Baru",
            command=self._create_room,
            font=(_FONT_PRIMARY[0], 15, "bold"),
            fg_color=_CLR_ACCENT, hover_color=_CLR_ACCENT2,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS, height=56,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))

        customtkinter.CTkLabel(
            self.find_frame, text="atau masukkan kode room",
            font=(_FONT_PRIMARY[0], 12), text_color=_CLR_MUTED,
        ).grid(row=1, column=0, columnspan=2, pady=(0, 8))

        code_row = customtkinter.CTkFrame(self.find_frame, fg_color="transparent")
        code_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        code_row.grid_columnconfigure(0, weight=1)

        self.code_entry = customtkinter.CTkEntry(
            code_row, placeholder_text="Kode Room (4 huruf)",
            font=(_FONT_PRIMARY[0], 16, "bold"), height=48,
            corner_radius=_CORNER_RADIUS, border_width=2, border_color=_CLR_BORDER,
            fg_color=_CLR_BG, text_color=_CLR_TEXT,
        )
        self.code_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        customtkinter.CTkButton(
            code_row, text="Join",
            command=self._join_by_code,
            font=(_FONT_PRIMARY[0], 12, "bold"),
            fg_color=_CLR_CARD, hover_color=_CLR_ACCENT,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS, height=48, width=90,
        ).grid(row=0, column=1)

        list_header = customtkinter.CTkFrame(self.find_frame, fg_color="transparent")
        list_header.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        list_header.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(
            list_header, text="Room Tersedia",
            font=(_FONT_PRIMARY[0], 13, "bold"), text_color=_CLR_MUTED,
        ).grid(row=0, column=0, sticky="w")
        customtkinter.CTkButton(
            list_header, text="Refresh",
            command=self._refresh_rooms,
            font=(_FONT_PRIMARY[0], 11, "bold"),
            fg_color=_CLR_CARD, hover_color=_CLR_SUBTLE_BORDER,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS_SMALL, height=34, width=90,
        ).grid(row=0, column=1)

        self.room_list_frame = customtkinter.CTkScrollableFrame(
            self.find_frame, fg_color=_CLR_CARD, corner_radius=_CORNER_RADIUS, border_width=1, border_color=_CLR_SUBTLE_BORDER, height=160,
        )
        self.room_list_frame.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.room_list_frame.grid_columnconfigure(0, weight=1)

        self._rooms_placeholder = customtkinter.CTkLabel(
            self.room_list_frame, text="Memuat...",
            font=(_FONT_PRIMARY[0], 13, "bold"), text_color=_CLR_MUTED,
        )
        self._rooms_placeholder.grid(row=0, column=0, pady=24)

        # ── Lobby panel (hidden until joined) ──
        self.lobby_frame = customtkinter.CTkFrame(self, fg_color=_CLR_BG)
        self.lobby_frame.grid_columnconfigure(0, weight=1)

        code_card = customtkinter.CTkFrame(self.lobby_frame, fg_color=_CLR_CARD, corner_radius=_CORNER_RADIUS, border_width=2, border_color=_CLR_ACCENT)
        code_card.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        code_card.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(
            code_card, text="KODE ROOM",
            font=(_FONT_PRIMARY[0], 12, "bold"), text_color=_CLR_MUTED,
        ).grid(row=0, column=0, pady=(16, 0))
        self.code_display = customtkinter.CTkLabel(
            code_card, text="----",
            font=(_FONT_MONO[0], 44, "bold"), text_color=_CLR_ACCENT,
        )
        self.code_display.grid(row=1, column=0, pady=(6, 8))
        customtkinter.CTkButton(
            code_card, text="Salin Kode",
            command=self._copy_code,
            font=(_FONT_PRIMARY[0], 11, "bold"),
            fg_color=_CLR_SUBTLE_BORDER, hover_color=_CLR_ACCENT,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS_SMALL, height=34, width=110,
        ).grid(row=2, column=0, pady=(0, 16))

        players_row = customtkinter.CTkFrame(self.lobby_frame, fg_color="transparent")
        players_row.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        players_row.grid_columnconfigure((0, 1), weight=1)

        def _player_card(parent, col):
            card = customtkinter.CTkFrame(parent, fg_color=_CLR_CARD, corner_radius=_CORNER_RADIUS, border_width=1, border_color=_CLR_SUBTLE_BORDER)
            card.grid(row=0, column=col, padx=(0, 8) if col == 0 else (8, 0), sticky="nsew")
            card.grid_columnconfigure(0, weight=1)
            return card

        p1 = _player_card(players_row, 0)
        customtkinter.CTkLabel(p1, text="PLAYER 1", font=(_FONT_PRIMARY[0], 11, "bold"), text_color=_CLR_MUTED).grid(row=0, column=0, pady=(14, 4))
        self.p1_name = customtkinter.CTkLabel(p1, text="...", font=(_FONT_PRIMARY[0], 18, "bold"), text_color=_CLR_TEXT)
        self.p1_name.grid(row=1, column=0, pady=(0, 6))
        self.p1_status = customtkinter.CTkLabel(
            p1, text="Menunggu", font=(_FONT_PRIMARY[0], 11, "bold"),
            text_color=_CLR_MUTED, fg_color=_CLR_BG, corner_radius=_CORNER_RADIUS_SMALL, width=110, height=26,
        )
        self.p1_status.grid(row=2, column=0, pady=(0, 14))

        p2 = _player_card(players_row, 1)
        customtkinter.CTkLabel(p2, text="PLAYER 2", font=(_FONT_PRIMARY[0], 11, "bold"), text_color=_CLR_MUTED).grid(row=0, column=0, pady=(14, 4))
        self.p2_name = customtkinter.CTkLabel(p2, text="Menunggu...", font=(_FONT_PRIMARY[0], 18, "bold"), text_color=_CLR_MUTED)
        self.p2_name.grid(row=1, column=0, pady=(0, 6))
        self.p2_status = customtkinter.CTkLabel(
            p2, text="", font=(_FONT_PRIMARY[0], 11, "bold"),
            text_color=_CLR_MUTED, fg_color=_CLR_BG, corner_radius=_CORNER_RADIUS_SMALL, width=110, height=26,
        )
        self.p2_status.grid(row=2, column=0, pady=(0, 14))

        self.lobby_msg = customtkinter.CTkLabel(
            self.lobby_frame, text="",
            font=(_FONT_PRIMARY[0], 12), text_color=_CLR_MUTED,
        )
        self.lobby_msg.grid(row=2, column=0, pady=(0, 8))

        btn_row = customtkinter.CTkFrame(self.lobby_frame, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="ew")
        btn_row.grid_columnconfigure(0, weight=1)

        self.ready_btn = customtkinter.CTkButton(
            btn_row, text="SIAP",
            command=self._mark_ready,
            font=(_FONT_PRIMARY[0], 18, "bold"),
            fg_color=_CLR_CARD, hover_color=_CLR_ACCENT,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS, height=58,
        )
        self.ready_btn.grid(row=0, column=0, padx=(0, 10), sticky="ew")

        customtkinter.CTkButton(
            btn_row, text="Keluar",
            command=self._leave_room,
            font=(_FONT_PRIMARY[0], 12, "bold"),
            fg_color=_CLR_DANGER, hover_color=_CLR_DANGER_HOVER,
            text_color=_CLR_TEXT,
            corner_radius=_CORNER_RADIUS, height=58, width=110,
        ).grid(row=0, column=1)

    # ── Panel switching ───────────────────────────────────────────────────────

    def _show_find(self):
        self.lobby_frame.grid_remove()
        self.find_frame.grid(row=2, column=0, sticky="nsew", padx=20, pady=16)

    def _show_lobby(self, room: dict):
        self.find_frame.grid_remove()
        self.lobby_frame.grid(row=2, column=0, sticky="nsew", padx=20, pady=16)
        self._i_am_ready = False
        self.ready_btn.configure(text="SIAP", fg_color=_CLR_CARD, state="normal")
        self._update_lobby(room)
        self._start_polling()

    # ── Generic async API call helper ─────────────────────────────────────────

    def _api_call(self, fn, on_success, on_error=None):
        def _worker():
            try:
                result = fn()
                try:
                    self.after(0, lambda r=result: on_success(r))
                except Exception:
                    pass
            except Exception as e:
                msg = str(e)
                cb = on_error if on_error else self._show_err
                try:
                    self.after(0, lambda m=msg: cb(m))
                except Exception:
                    pass
        threading.Thread(target=_worker, daemon=True).start()

    def _show_err(self, msg):
        import tkinter.messagebox as mb
        mb.showerror("Error", msg, parent=self)

    # ── Room list ─────────────────────────────────────────────────────────────

    def _refresh_rooms(self):
        import requests as _r
        def _fetch():
            resp = _r.get(f"{API_SERVER_URL}/api/rooms", timeout=5)
            resp.raise_for_status()
            return resp.json().get("rooms", [])
        self._api_call(_fetch, self._update_room_list)

    def _update_room_list(self, rooms: list):
        for w in self.room_list_frame.winfo_children():
            w.destroy()
        self.room_list_frame.grid_columnconfigure(0, weight=1)
        if not rooms:
            customtkinter.CTkLabel(
                self.room_list_frame, text="Belum ada room",
                font=(_FONT_PRIMARY[0], 13), text_color=_CLR_MUTED,
            ).grid(row=0, column=0, pady=24)
            return
        for i, r in enumerate(rooms):
            row = customtkinter.CTkFrame(
                self.room_list_frame, fg_color=_CLR_BG, corner_radius=_CORNER_RADIUS_SMALL,
                border_width=1, border_color=_CLR_SUBTLE_BORDER,
            )
            row.grid(row=i, column=0, sticky="ew", pady=4, padx=4)
            row.grid_columnconfigure(1, weight=1)
            customtkinter.CTkLabel(row, text=r["id"], font=(_FONT_PRIMARY[0], 18, "bold"), text_color=_CLR_ACCENT, width=64).grid(row=0, column=0, padx=(12, 8), pady=8)
            customtkinter.CTkLabel(row, text=r.get("player1_name", "—"), font=(_FONT_PRIMARY[0], 13), text_color=_CLR_TEXT).grid(row=0, column=1, sticky="w")
            player_count = 2 if r.get("player2_name") else (1 if r.get("player1_name") else 0)
            customtkinter.CTkLabel(row, text=f"{player_count}/2", font=(_FONT_PRIMARY[0], 12), text_color=_CLR_MUTED, width=32).grid(row=0, column=2)
            code = r["id"]
            customtkinter.CTkButton(
                row, text="Join",
                command=lambda c=code: self._join_by_id(c),
                font=(_FONT_PRIMARY[0], 12, "bold"),
                fg_color=_CLR_ACCENT, hover_color=_CLR_ACCENT2,
                corner_radius=_CORNER_RADIUS_SMALL, height=38, width=70,
            ).grid(row=0, column=3, padx=(8, 12), pady=8)

    # ── Create / Join ─────────────────────────────────────────────────────────

    def _create_room(self):
        import requests as _r
        def _do():
            resp = _r.post(f"{API_SERVER_URL}/api/rooms", json={"player_name": nick_name}, timeout=5)
            resp.raise_for_status()
            return resp.json()
        self._api_call(_do, self._on_joined, self._show_err)

    def _join_by_code(self):
        code = self.code_entry.get().strip().upper()
        if len(code) != 4:
            self._show_err("Kode room harus 4 karakter (contoh: ABCD)")
            return
        self._join_by_id(code)

    def _join_by_id(self, code: str):
        import requests as _r
        def _do():
            resp = _r.post(f"{API_SERVER_URL}/api/rooms/{code}/join", json={"player_name": nick_name}, timeout=5)
            resp.raise_for_status()
            return resp.json()
        self._api_call(_do, self._on_joined, self._show_err)

    def _on_joined(self, room: dict):
        self.room_code  = room["id"]
        self.player_num = 1 if room.get("player1_name") == nick_name else 2
        self._show_lobby(room)

    # ── Lobby actions ─────────────────────────────────────────────────────────

    def _mark_ready(self):
        if self._i_am_ready:
            return
        import requests as _r
        def _do():
            resp = _r.post(f"{API_SERVER_URL}/api/rooms/{self.room_code}/ready", json={"player_name": nick_name}, timeout=5)
            resp.raise_for_status()
            return resp.json()
        self._api_call(_do, self._on_ready_response, self._show_err)

    def _on_ready_response(self, room: dict):
        self._i_am_ready = True
        self.ready_btn.configure(text="SIAP!", fg_color=_CLR_SUBTLE_BORDER, state="disabled")
        self._update_lobby(room)
        if room.get("status") == "playing":
            self._on_game_start()

    def _leave_room(self):
        if not self.room_code:
            self._stop_polling()
            self._show_find()
            self._refresh_rooms()
            return
        import requests as _r
        code = self.room_code
        self.room_code = None
        self._stop_polling()
        def _do():
            try:
                _r.post(f"{API_SERVER_URL}/api/rooms/{code}/leave", json={"player_name": nick_name}, timeout=5)
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()
        self._show_find()
        self._refresh_rooms()

    def _copy_code(self):
        if self.room_code:
            self.clipboard_clear()
            self.clipboard_append(self.room_code)

    # ── Lobby UI update ───────────────────────────────────────────────────────

    def _update_lobby(self, room: dict):
        self.code_display.configure(text=room.get("id", "----"))
        p1  = room.get("player1_name") or "—"
        p2  = room.get("player2_name")
        r1  = room.get("player1_ready", False)
        r2  = room.get("player2_ready", False)
        st  = room.get("status", "waiting")

        self.p1_name.configure(text=p1)
        self.p1_status.configure(
            text="SIAP" if r1 else "Belum Siap",
            text_color=_CLR_SUCCESS if r1 else _CLR_MUTED,
            fg_color=_CLR_SUCCESS_BG if r1 else _CLR_BG,
        )

        if p2:
            self.p2_name.configure(text=p2, text_color=_CLR_TEXT)
            self.p2_status.configure(
                text="SIAP" if r2 else "Belum Siap",
                text_color=_CLR_SUCCESS if r2 else _CLR_MUTED,
                fg_color=_CLR_SUCCESS_BG if r2 else _CLR_BG,
            )
        else:
            self.p2_name.configure(text="Menunggu pemain...", text_color=_CLR_MUTED)
            self.p2_status.configure(text="", fg_color=_CLR_BG)

        if st == "playing":
            msg = "Kedua pemain siap! Game dimulai..."
        elif st == "ready" and r1 and not r2:
            msg = "Menunggu Player 2 tekan SIAP..."
        elif st == "ready" and not r1 and r2:
            msg = "Menunggu Player 1 tekan SIAP..."
        elif st == "ready":
            msg = "Kedua pemain hadir. Tekan SIAP!"
        else:
            msg = "Menunggu pemain lain bergabung..."
        self.lobby_msg.configure(text=msg)

    # ── Polling ───────────────────────────────────────────────────────────────

    def _start_polling(self):
        self._polling = True
        self.after(2000, self._poll)

    def _stop_polling(self):
        self._polling = False

    def _poll(self):
        if not self._polling or not self.room_code:
            return
        import requests as _r
        code = self.room_code
        def _worker():
            try:
                resp = _r.get(f"{API_SERVER_URL}/api/rooms/{code}", timeout=3)
                data = None if resp.status_code == 404 else resp.json()
            except Exception:
                data = "__error__"
            try:
                self.after(0, lambda d=data: self._handle_poll(d))
            except Exception:
                pass
        threading.Thread(target=_worker, daemon=True).start()

    def _handle_poll(self, room):
        if not self._polling:
            return
        if room is None:
            self._stop_polling()
            self.room_code = None
            import tkinter.messagebox as mb
            mb.showinfo("Info", "Room dihapus oleh server", parent=self)
            self._show_find()
            self._refresh_rooms()
            return
        if room == "__error__":
            if self._polling:
                self.after(3000, self._poll)
            return
        self._update_lobby(room)
        if room.get("status") == "playing":
            self._on_game_start()
        elif self._polling:
            self.after(2000, self._poll)

    def _on_game_start(self):
        self._stop_polling()
        self.room_ready = True
        self.destroy()

    def _on_close(self):
        self._stop_polling()
        if self.room_code:
            import requests as _r
            try:
                _r.post(f"{API_SERVER_URL}/api/rooms/{self.room_code}/leave",
                        json={"player_name": nick_name}, timeout=3)
            except Exception:
                pass
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        if not nick_name:
            raise SystemExit(0)

        # Competition: go to App_Room first (duel) — skip if tournament match already assigned
        if PC_MODE == "competition":
            IS_MULTIPLAYER = True
            if TOURNAMENT_MODE and TOURNAMENT_ROOM_CODE:
                # Tournament cup: use pre-assigned room code, skip lobby
                CURRENT_ROOM_CODE = TOURNAMENT_ROOM_CODE
                PLAYER_NUM = 1 if TOURNAMENT_IS_P1 else 2
                print(f">>> Tournament mode — joining room '{CURRENT_ROOM_CODE}' as Player {PLAYER_NUM}")
            else:
                app_room = App_Room()
                app_room.mainloop()
                if not app_room.room_ready:
                    raise SystemExit(0)
                CURRENT_ROOM_CODE = app_room.room_code
                PLAYER_NUM        = app_room.player_num

        # Training or after room: launch TimeIn
        app = TimeIn()
        app.after(0, lambda: app.state('zoomed'))
        app.mainloop()
    except SystemExit:
        raise
    except Exception:
        print(">>> Application crashed. Full traceback:")
        traceback.print_exc()
        raise SystemExit(1)
