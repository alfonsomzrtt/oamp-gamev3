# import json
# import threading
# import time
# from  pathlib import Path 
# from typing import Any, Callable, Dict, Optional 

# import requests 

# _ROOT = Path(__file__).resolve().resolve().parents[1]
# _RESULTS_DIR = _ROOT / "results"
# _RESULTS_DIR.mkdir(exist_ok=True)

# _MAX_RETRIES = 3
# _RETRY_BACKOFF = [1, 2, 4]

# def _save_local_backup(filename: str, payload: Dict[str, Any]) -> None: 
#     path = _RESULTS_DIR / filename
#     with open(path, "w", encoding="utf-8") as f:
#         json.dump(payload, f, indent=2, default=str)

# def api_post(url: str, payload: Dict[str, Any], timeout: int = 10) -> bool: 
#     for attempt in range(_MAX_RETRIES):
#         try: 
#             r = requests.post(url, json=payload, timeout=timeout)
#             if 200 <= r.status_code < 300:
#                  return True
#         except Exception:
#              pass
#         if attempt < _MAX_RETRIES - 1: 
#            time.sleep(_RETRY_BACKOFF[attempt])
#     return False

# class ApiClient:
#     def __init__(self, base_url: str):
#         self.base_url = base_url.rstrip("/") if base_url else ""

#     def post(self, path: str, payload: Dict[str, Any], timeout: int = 10) -> bool:
#         if not self.base_url:
#             _save_local_backup(f"offline_{int(time.time())}.json", payload)
#             return False
#         return api_post(f"{self.base_url.rstrip('/')}/{path.lstrip('/')}", payload, timeout=timeout)

#     def fire_event(self, path: str, payload: Dict[str, Any]) -> threading.Thread:
#         def _do():
#             ok = self.post(path, payload, timeout=5)
#             if not ok:
#                 _save_local_backup(f"event_{int(time.time())}.json", payload)
#         t = threading.Thread(target=_do, daemon=True)
#         t.start()
#         return t

#     def get_json(self, path: str, timeout: int = 5) -> Optional[Dict[str, Any]]:
#         if not self.base_url:
#             return None
#         try:
#             r = requests.get(f"{self.base_url.rstrip('/')}/{path.lstrip('/')}", timeout=timeout)
#             if r.status_code == 200:
#                 return r.json()
#         except Exception:
#             pass
#         return None
