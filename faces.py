"""Face detection (SCRFD) + recognition (ArcFace) directly on onnxruntime.

No `insightface` package needed (it needs a C++ compiler on Windows). The ONNX
weights are the official InsightFace ones:
  det_10g.onnx   - SCRFD-10G detector with 5 landmarks (accurate, handles angles)
  det_500m.onnx  - SCRFD-500M detector (very fast, weaker on small faces)
  w600k_r50.onnx - ArcFace ResNet-50 trained on WebFace600K (best accuracy)
  w600k_mbf.onnx - ArcFace MobileFaceNet (fast, lower accuracy)
"""
import os
from dataclasses import dataclass

import cv2
import numpy as np
import onnxruntime as ort

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

# ArcFace 112x112 reference landmarks (eyes, nose, mouth corners)
ARCFACE_DST = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


def _session(name, threads):
    path = name if os.path.isabs(name) else os.path.join(MODELS_DIR, name)
    if not path.endswith(".onnx"):
        path += ".onnx"
    if not os.path.exists(path):
        raise FileNotFoundError(f"Model file missing: {path}")
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.intra_op_num_threads = max(1, int(threads))
    opts.inter_op_num_threads = 1
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    # SCRFD exports declare fixed output shapes; with dynamic input sizes ORT
    # warns on every call. The outputs are correct, so only log real errors.
    opts.log_severity_level = 3
    return ort.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])


@dataclass
class Face:
    bbox: np.ndarray            # x1, y1, x2, y2 in full-frame pixels
    kps: np.ndarray             # 5x2 landmarks in full-frame pixels
    score: float

    @property
    def width(self):
        return float(self.bbox[2] - self.bbox[0])

    @property
    def height(self):
        return float(self.bbox[3] - self.bbox[1])

    def pose(self):
        """Rough (yaw, pitch) ratios from the 5 landmarks.

        yaw   ~ 0 frontal, > 0 person turned to THEIR left, < 0 to their right.
              |yaw| 0.25 ~ 20-25 deg, 0.5 ~ 40-45 deg.
        pitch ~ 0 frontal, < 0 looking up, > 0 looking down.
        """
        le, re, nose, lm, rm = self.kps
        eye_mid = (le + re) / 2
        mouth_mid = (lm + rm) / 2
        eye_dist = max(1.0, float(np.linalg.norm(re - le)))
        # use face height too so strong profiles (eyes merged) still read large
        span = max(eye_dist, float(np.linalg.norm(mouth_mid - eye_mid)))
        yaw = float(nose[0] - eye_mid[0]) / span
        vertical = max(1.0, float(mouth_mid[1] - eye_mid[1]))
        pitch = float(nose[1] - eye_mid[1]) / vertical - 0.63
        return yaw, pitch


def _nms(dets, thresh):
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(ovr <= thresh)[0] + 1]
    return keep


class Detector:
    """SCRFD face detector with dynamic input size (keeps camera aspect ratio)."""

    def __init__(self, model="det_10g", threads=3):
        self.session = _session(model, threads)
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [o.name for o in self.session.get_outputs()]
        n = len(self.output_names)
        self.use_kps = n in (9, 15)
        self.fmc = 3 if n in (6, 9) else 5
        self.strides = [8, 16, 32] if self.fmc == 3 else [8, 16, 32, 64, 128]
        self.num_anchors = 2 if self.fmc == 3 else 1
        self._anchor_cache = {}

    def _anchors(self, height, width, stride):
        key = (height, width, stride)
        if key not in self._anchor_cache:
            centers = np.stack(np.mgrid[:height, :width][::-1], axis=-1).astype(np.float32)
            centers = (centers * stride).reshape((-1, 2))
            if self.num_anchors > 1:
                centers = np.stack([centers] * self.num_anchors, axis=1).reshape((-1, 2))
            self._anchor_cache[key] = centers
        return self._anchor_cache[key]

    def detect(self, img, det_width=640, threshold=0.5, nms_thresh=0.4, max_faces=0):
        """Returns list[Face] in the coordinates of `img`."""
        h, w = img.shape[:2]
        scale = det_width / float(max(w, h))
        new_w = max(32, int(round(w * scale)))
        new_h = max(32, int(round(h * scale)))
        in_w = (new_w + 31) // 32 * 32
        in_h = (new_h + 31) // 32 * 32
        canvas = np.zeros((in_h, in_w, 3), dtype=np.uint8)
        canvas[:new_h, :new_w] = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        det_scale = new_h / float(h)

        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 128, (in_w, in_h),
                                     (127.5, 127.5, 127.5), swapRB=True)
        outs = self.session.run(self.output_names, {self.input_name: blob})

        scores_l, boxes_l, kps_l = [], [], []
        for idx, stride in enumerate(self.strides):
            scores = outs[idx].reshape(-1)
            bbox_preds = outs[idx + self.fmc].reshape(-1, 4) * stride
            centers = self._anchors(in_h // stride, in_w // stride, stride)
            pos = np.where(scores >= threshold)[0]
            if not len(pos):
                continue
            c = centers[pos]
            b = bbox_preds[pos]
            boxes = np.stack([c[:, 0] - b[:, 0], c[:, 1] - b[:, 1],
                              c[:, 0] + b[:, 2], c[:, 1] + b[:, 3]], axis=-1)
            scores_l.append(scores[pos])
            boxes_l.append(boxes)
            if self.use_kps:
                k = outs[idx + self.fmc * 2].reshape(-1, 10)[pos] * stride
                k = k.reshape(-1, 5, 2) + c[:, None, :]
                kps_l.append(k)

        if not scores_l:
            return []
        scores = np.concatenate(scores_l)
        boxes = np.concatenate(boxes_l) / det_scale
        kps = np.concatenate(kps_l) / det_scale if self.use_kps else None
        dets = np.hstack([boxes, scores[:, None]]).astype(np.float32)
        keep = _nms(dets, nms_thresh)
        faces = []
        for i in keep:
            box = dets[i, :4].copy()
            box[[0, 2]] = np.clip(box[[0, 2]], 0, w - 1)
            box[[1, 3]] = np.clip(box[[1, 3]], 0, h - 1)
            faces.append(Face(bbox=box, kps=kps[i].copy() if kps is not None else None,
                              score=float(dets[i, 4])))
        faces.sort(key=lambda f: f.width * f.height, reverse=True)
        if max_faces > 0:
            faces = faces[:max_faces]
        return faces


def align(img, kps, size=112):
    M, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), ARCFACE_DST * (size / 112.0),
                                       method=cv2.LMEDS)
    if M is None:
        return None
    return cv2.warpAffine(img, M, (size, size), flags=cv2.INTER_LINEAR, borderValue=0)


def sharpness(crop):
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


class Embedder:
    """ArcFace 512-d embedding; batches multiple faces in one call."""

    def __init__(self, model="w600k_r50", threads=3):
        self.session = _session(model, threads)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        shape = self.session.get_inputs()[0].shape
        self.size = int(shape[2]) if isinstance(shape[2], int) else 112
        self.dynamic_batch = not isinstance(shape[0], int)

    def embed_crops(self, crops):
        if not crops:
            return np.zeros((0, 512), dtype=np.float32)
        blobs = cv2.dnn.blobFromImages(crops, 1.0 / 127.5, (self.size, self.size),
                                       (127.5, 127.5, 127.5), swapRB=True)
        if self.dynamic_batch:
            feats = self.session.run([self.output_name], {self.input_name: blobs})[0]
        else:
            feats = np.concatenate([
                self.session.run([self.output_name], {self.input_name: blobs[i:i + 1]})[0]
                for i in range(len(crops))])
        feats = feats.astype(np.float32)
        feats /= np.maximum(np.linalg.norm(feats, axis=1, keepdims=True), 1e-12)
        return feats
