# from typing import Any, Optional
# import os 

# def _device():
#     try:
#         import torch
#         return torch.device("cuda" if torch.cuda.is_available() else "cpu")
#     except Exception:
#         return "cpu"

# class ModelWrapper:
#         def __init__(self, model, use_ultralytics: bool, device):
#             self.model = model
#             self.use_ultralytics = use_ultralytics
#             self.device = device

#         def predict(self, frame, size: int = 320):
#             #frame: numpy array (RGB). Return list-like of detections.
#             try:
#                 if self.use_ultralytics:
#                     res = self.model(frame, verbose=False)
#                     if len(res) and hasattr(res[0], "boxes"):
#                         boxes = []
#                         for b in res[0].boxes:
#                             x1, y1, x2, y2 = b.xyxy[0].cpu().numpy()
#                             conf = float(b.conf[0].cpu().numpy())
#                             cls = int(b.cls[0].cpu().numpy())
#                             boxes.append([x1, y1, x2, y2, conf, cls])
#                         return boxes
#                     return []
#                 else:
#                     results = self.model(frame, size=size)
#                     df = results.pandas().xyxy[0]
#                     return df.values.tolist()
#             except Exception:
#                 return []

#             def __call__(self, *args, **kwargs):
#                 """Allow the wrapper to be called like the original model for compatibility."""
#                 return self.predict(*args, **kwargs)

# def load_model(path: str, use_bantal: bool = False, device: Optional[Any] = None):
#     device = device or _device()
#     if use_bantal:
#         try:
#             from ultralytics import YOLO
#             model = YOLO(path)
#             model.to(device)
#             return ModelWrapper(model, use_ultralytics=True, device=device)
#         except Exception:
#             # fall back to yolov5 attempt_load
#             pass
#     # fallback: yolov5 attempt_load via local repo
#     import sys
#     repo = os.path.join(os.path.dirname(__file__), "..", "MODEL", "yolov5")
#     sys.path.insert(0, repo)
#     try:
#         from models.experimental import attempt_load
#         model = attempt_load(path, device=device)
#         model.to(device)
#         return ModelWrapper(model, use_ultralytics=False, device=device)
#     except Exception:
#         raise            