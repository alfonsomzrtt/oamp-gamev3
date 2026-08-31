# from __future__ import annotations

# import os
# from dataclasses import dataclass
# from pathlib import Path
# from typing import Dict, Any

# from dotenv import load_dotenv


# def _env_path() -> Path:
#     # project root (one level above src/)
#     return Path(__file__).resolve().parents[1]


# def sanitize_env_value(raw_value: Any) -> str:
#     """Trim env values and remove inline comments (e.g., '1a,2b # notes')."""
#     if raw_value is None:
#         return ""
#     value = str(raw_value).strip()
#     if "#" in value:
#         value = value.split("#", 1)[0].strip()
#     return value


# def parse_custom_levels(custom_level_str: str) -> Dict[int, str]:
#     """Parse and validate custom levels from string format.

#     Examples: '1a,2b,3c' -> {1: '1a', 2: '2b', 3: '3c'}
#     Returns empty dict on invalid input.
#     """
#     s = sanitize_env_value(custom_level_str)
#     if not s or s == "[]":
#         return {}
#     try:
#         levels = [lvl.strip() for lvl in s.split(",") if lvl.strip()]
#         custom_levels: Dict[int, str] = {}
#         for level in levels:
#             if len(level) != 2:
#                 return {}
#             level_num = level[0]
#             variant = level[1].lower()
#             if not level_num.isdigit() or not (1 <= int(level_num) <= 8):
#                 return {}
#             if variant not in ("a", "b", "c", "d"):
#                 return {}
#             custom_levels[int(level_num)] = f"{level_num}{variant}"
#         return custom_levels
#     except Exception:
#         return {}


# @dataclass
# class Config:
#     # Display & UI
#     DISPLAY_HALF: bool = True
#     NORMAL_PATTERN: bool = True
#     BUTTON_MODE: bool = False
#     HIDE_CAMERA: bool = False

#     # Camera
#     CAMERA_INDEX: int = 0
#     CAMERA_MIRROR_X: bool = False
#     CAMERA_MIRROR_Y: bool = False
#     CAMERA_ZOOM: float = 1.0
#     CAMERA_BRIGHTNESS: int = 128
#     CAMERA_CONTRAST: int = 128
#     CAMERA_SATURATION: int = 128
#     CAMERA_CALIBRATION: bool = False

#     # Mode / game
#     CURRENT_MODE: str = "training"
#     MAX_LEVEL: int = 8

#     # API / networking
#     API_SERVER_URL: str = ""
#     ROOM_ID: str = "room_01"
#     PLAYER_NUM: int = 1

#     # Paths
#     BASE_DIR: Path = Path(".")
#     MODEL_BANTAL: bool = False
#     PATH_MODEL: str = ""

#     # Additional runtime tuning
#     THEME: str = "dark"
#     YOLO_SKIP_FRAMES: int = 2
#     MEDIAPIPE_SKIP_FRAMES: int = 2
#     DEBUG_MODE: bool = False

#     # Custom levels
#     CUSTOM_LEVEL_STR: str = ""
#     CUSTOM_LEVELS: Dict[int, str] = None

#     @classmethod
#     def load_from_env(cls) -> "Config":
#         root = _env_path()
#         env_file = root / ".env"
#         if env_file.exists():
#             load_dotenv(dotenv_path=env_file)

#         def g(key: str, default: Any = None) -> str:
#             return os.getenv(key, default)

#         cfg = cls()
#         cfg.BASE_DIR = root

#         cfg.DISPLAY_HALF = str(g("DISPLAY_HALF", "true")).lower() == "true"
#         cfg.NORMAL_PATTERN = str(g("NORMAL_PATTERN", "true")).lower() == "true"
#         cfg.BUTTON_MODE = str(g("BUTTON_MODE", "false")).lower() == "true"
#         cfg.HIDE_CAMERA = str(g("HIDE_CAMERA", "false")).lower() == "true"

#         try:
#             cfg.CAMERA_INDEX = int(g("CAMERA_INDEX", "0"))
#         except Exception:
#             cfg.CAMERA_INDEX = 0

#         cfg.CAMERA_MIRROR_X = str(g("CAMERA_MIRROR_X", "false")).lower() == "true"
#         cfg.CAMERA_MIRROR_Y = str(g("CAMERA_MIRROR_Y", "false")).lower() == "true"
#         try:
#             cz = float(g("CAMERA_ZOOM", "1.0"))
#             cfg.CAMERA_ZOOM = cz if 0.5 <= cz <= 3.0 else 1.0
#         except Exception:
#             cfg.CAMERA_ZOOM = 1.0

#         try:
#             cfg.CAMERA_BRIGHTNESS = int(g("CAMERA_BRIGHTNESS", "128"))
#         except Exception:
#             cfg.CAMERA_BRIGHTNESS = 128
#         try:
#             cfg.CAMERA_CONTRAST = int(g("CAMERA_CONTRAST", "128"))
#         except Exception:
#             cfg.CAMERA_CONTRAST = 128
#         try:
#             cfg.CAMERA_SATURATION = int(g("CAMERA_SATURATION", "128"))
#         except Exception:
#             cfg.CAMERA_SATURATION = 128

#         cfg.CAMERA_CALIBRATION = str(g("CAMERA_CALIBRATION", "false")).lower() == "true"

#         cfg.CURRENT_MODE = str(g("PC_MODE", "training")).lower()
#         try:
#             ml = int(g("MAX_LEVEL", "8"))
#             cfg.MAX_LEVEL = ml if 1 <= ml <= 8 else 8
#         except Exception:
#             cfg.MAX_LEVEL = 8

#         cfg.API_SERVER_URL = str(g("API_SERVER_URL", "")).rstrip("/")
#         cfg.ROOM_ID = str(g("ROOM_ID", "room_01"))
#         try:
#             cfg.PLAYER_NUM = int(g("PLAYER_NUM", "1"))
#         except Exception:
#             cfg.PLAYER_NUM = 1

#         cfg.MODEL_BANTAL = str(g("MODEL_BANTAL", "false")).lower() == "true"
#         # Default model path relative to project
#         cfg.PATH_MODEL = str(g("PATH_MODEL", os.path.join(str(root), "MODEL", "exp7", "weights", "best.pt")))

#         cfg.CUSTOM_LEVEL_STR = sanitize_env_value(g("CUSTOM_LEVEL", ""))
#         cfg.CUSTOM_LEVELS = parse_custom_levels(cfg.CUSTOM_LEVEL_STR)

#         # Additional runtime tuning
#         cfg.THEME = str(g("THEME", "dark")).lower()
#         try:
#             cfg.YOLO_SKIP_FRAMES = int(g("YOLO_SKIP_FRAMES", "2"))
#         except Exception:
#             cfg.YOLO_SKIP_FRAMES = 2
#         try:
#             cfg.MEDIAPIPE_SKIP_FRAMES = int(g("MEDIAPIPE_SKIP_FRAMES", "2"))
#         except Exception:
#             cfg.MEDIAPIPE_SKIP_FRAMES = 2
#         cfg.DEBUG_MODE = str(g("DEBUG_MODE", "false")).lower() == "true"

#         return cfg


# def save_env_value(key: str, value: str) -> None:
#     """Update or append a key=value pair in the .env file."""
#     env_path = _env_path() / ".env"
#     lines = []
#     if env_path.exists():
#         with open(env_path, "r", encoding="utf-8") as f:
#             lines = f.readlines()
#     updated = False
#     new_lines = []
#     for line in lines:
#         stripped = line.strip()
#         if stripped.split("=")[0] == key if "=" in stripped else False:
#             new_lines.append(f"{key}={value}\n")
#             updated = True
#         else:
#             new_lines.append(line)
#     if not updated:
#         new_lines.append(f"{key}={value}\n")
#     with open(env_path, "w", encoding="utf-8") as f:
#         f.writelines(new_lines)
