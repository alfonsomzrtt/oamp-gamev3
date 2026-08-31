# from __future__ import annotations

# import logging
# import sys
# import threading
# import traceback
# from datetime import datetime
# from pathlib import Path


# def _log_file_path() -> Path:
#     # store runtime errors at project root
#     return Path(__file__).resolve().parents[1] / "runtime_errors.log"


# def log_exception(context: str, exc_type, exc_value, exc_tb) -> None:
#     """Safe exception logger that writes to stderr and a local file."""
#     timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#     header = f"[{timestamp}] {context}\n"
#     trace = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
#     payload = f"{header}{trace}\n"

#     # Try file first (best-effort)
#     try:
#         with _log_file_path().open("a", encoding="utf-8") as f:
#             f.write(payload)
#     except Exception:
#         pass

#     # Then stderr (use __stderr__ to avoid redirection surprises)
#     try:
#         sys.__stderr__.write(payload)
#         sys.__stderr__.flush()
#     except Exception:
#         try:
#             sys.__stderr__.buffer.write(payload.encode("utf-8", "backslashreplace"))
#             sys.__stderr__.buffer.flush()
#         except Exception:
#             pass


# def _install_excepthooks():
#     def _global_excepthook(exc_type, exc_value, exc_tb):
#         log_exception("Unhandled exception (main thread)", exc_type, exc_value, exc_tb)

#     def _thread_excepthook(args):
#         thread_name = args.thread.name if getattr(args, "thread", None) else "unknown"
#         log_exception(f"Unhandled exception (thread: {thread_name})", args.exc_type, args.exc_value, args.exc_traceback)

#     sys.excepthook = _global_excepthook
#     if hasattr(threading, "excepthook"):
#         threading.excepthook = _thread_excepthook


# def setup_logging(level: int = logging.INFO) -> logging.Logger:
#     """Configure a module logger and install safe excepthooks."""
#     logger = logging.getLogger("oamp")
#     if not logger.handlers:
#         handler = logging.StreamHandler(sys.stderr)
#         fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
#         handler.setFormatter(fmt)
#         logger.addHandler(handler)
#     logger.setLevel(level)

#     _install_excepthooks()
#     return logger
