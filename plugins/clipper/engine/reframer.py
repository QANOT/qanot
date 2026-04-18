"""Smart 9:16 reframe with face/person tracking.

Strategy:
  1. Sample frames at a fixed interval (4fps is enough for smooth tracking).
  2. For each sampled frame:
     - MediaPipe face detection → bounding box centroids
     - If no face → YOLOv8 person detection as fallback
     - If no person → use previous frame's centroid (keep crop stable)
  3. Smooth centroids with EMA (no jitter).
  4. Build ffmpeg `crop` filter with `x/y = expr(t)` referencing a keyframe
     table sent via `-filter_complex` or a sendcmd file.

This is the production approach — same pattern as openshorts, without the
Node/Remotion overhead.

Deps (optional): mediapipe, opencv-python, ultralytics (YOLOv8)
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from engine.models import Moment, SourceMedia
from engine.source import _run

logger = logging.getLogger(__name__)


# Tracking cadence: sample every N seconds of source video
SAMPLE_INTERVAL_S = 0.25  # 4 fps = smooth enough for panning
# Smoothing: exponential moving average strength (0 = no smooth, 1 = no motion)
EMA_ALPHA = 0.25
# Max per-frame shift in pixels (prevents jumpy crops on speaker switch)
MAX_SHIFT_PER_FRAME = 40


@dataclass
class _CropWindow:
    """One crop window at time t."""
    t_s: float
    x: int  # top-left x in source frame
    y: int
    w: int  # crop width
    h: int


def _check_deps() -> tuple[bool, str]:
    """Check that MediaPipe + cv2 are installed. Returns (ok, msg)."""
    missing: list[str] = []
    try:
        import cv2  # noqa: F401
    except ImportError:
        missing.append("opencv-python")
    try:
        import mediapipe  # noqa: F401
    except ImportError:
        missing.append("mediapipe")
    if missing:
        return False, f"smart reframe needs: pip install {' '.join(missing)}"
    return True, ""


def _detect_faces_sync(frame, mp_face) -> list[tuple[int, int, int, int]]:
    """Return list of face bboxes (x, y, w, h) in frame pixels."""
    import cv2
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w = frame.shape[:2]
    results = mp_face.process(rgb)
    boxes: list[tuple[int, int, int, int]] = []
    if results.detections:
        for det in results.detections:
            rbb = det.location_data.relative_bounding_box
            bx = max(0, int(rbb.xmin * w))
            by = max(0, int(rbb.ymin * h))
            bw = int(rbb.width * w)
            bh = int(rbb.height * h)
            if bw > 20 and bh > 20:  # filter tiny detections
                boxes.append((bx, by, bw, bh))
    return boxes


_yolo_model = None


def _get_yolo():
    """Lazy-load YOLOv8n (nano, ~6MB). Returns None if ultralytics not installed."""
    global _yolo_model
    if _yolo_model is not None:
        return _yolo_model
    try:
        from ultralytics import YOLO
    except ImportError:
        return None
    try:
        _yolo_model = YOLO("yolov8n.pt")  # auto-downloads first run
        return _yolo_model
    except Exception as e:
        logger.warning("YOLOv8 load failed: %s", e)
        return None


def _detect_persons_sync(frame) -> list[tuple[int, int, int, int]]:
    """YOLOv8 person detection fallback. Returns list of (x, y, w, h) bboxes."""
    model = _get_yolo()
    if model is None:
        return []
    try:
        results = model(frame, classes=[0], verbose=False)  # class 0 = person in COCO
    except Exception as e:
        logger.debug("YOLOv8 inference failed: %s", e)
        return []
    boxes: list[tuple[int, int, int, int]] = []
    for r in results:
        if r.boxes is None:
            continue
        for b in r.boxes.xyxy.cpu().numpy():
            x1, y1, x2, y2 = [int(v) for v in b]
            boxes.append((x1, y1, x2 - x1, y2 - y1))
    return boxes


def _pick_best_box(boxes: list[tuple[int, int, int, int]]) -> tuple[int, int] | None:
    """From multiple detections, pick the largest (likely main speaker).
    Returns (cx, cy) centroid in source pixels, or None.
    """
    if not boxes:
        return None
    biggest = max(boxes, key=lambda b: b[2] * b[3])
    bx, by, bw, bh = biggest
    return bx + bw // 2, by + bh // 2


def _track_sync(
    source_path: str,
    start_s: float,
    end_s: float,
    source_w: int,
    source_h: int,
    crop_w: int,
    crop_h: int,
) -> list[_CropWindow]:
    """Sample frames + detect faces/persons + smooth → return crop windows."""
    import cv2
    import mediapipe as mp

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        raise RuntimeError(f"cv2 could not open {source_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    # Total frames in the clip range
    start_frame = int(start_s * fps)
    end_frame = int(end_s * fps)
    sample_every_n = max(1, int(fps * SAMPLE_INTERVAL_S))

    mp_face = mp.solutions.face_detection.FaceDetection(
        model_selection=1,  # 1 = full-range (good for podcasts)
        min_detection_confidence=0.5,
    )

    # Fallback centroid if nothing detected
    default_cx = source_w // 2
    default_cy = source_h // 2

    windows: list[_CropWindow] = []
    smoothed_cx = default_cx
    smoothed_cy = default_cy

    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    frame_idx = start_frame

    while frame_idx <= end_frame:
        ok, frame = cap.read()
        if not ok:
            break

        if (frame_idx - start_frame) % sample_every_n == 0:
            # Try face first
            face_boxes = _detect_faces_sync(frame, mp_face)
            centroid = _pick_best_box(face_boxes)

            if centroid is None:
                # Fallback: YOLO person detection
                person_boxes = _detect_persons_sync(frame)
                centroid = _pick_best_box(person_boxes)

            if centroid is None:
                # Nothing detected — keep last smoothed position
                target_cx, target_cy = smoothed_cx, smoothed_cy
            else:
                target_cx, target_cy = centroid

            # EMA smoothing
            new_cx = int(smoothed_cx * (1 - EMA_ALPHA) + target_cx * EMA_ALPHA)
            new_cy = int(smoothed_cy * (1 - EMA_ALPHA) + target_cy * EMA_ALPHA)

            # Clamp per-frame shift
            new_cx = max(smoothed_cx - MAX_SHIFT_PER_FRAME,
                         min(smoothed_cx + MAX_SHIFT_PER_FRAME, new_cx))
            new_cy = max(smoothed_cy - MAX_SHIFT_PER_FRAME,
                         min(smoothed_cy + MAX_SHIFT_PER_FRAME, new_cy))

            smoothed_cx, smoothed_cy = new_cx, new_cy

            # Compute crop window clamped to source
            x = max(0, min(source_w - crop_w, smoothed_cx - crop_w // 2))
            y = max(0, min(source_h - crop_h, smoothed_cy - crop_h // 2))
            t_local = (frame_idx - start_frame) / fps
            windows.append(_CropWindow(t_s=t_local, x=x, y=y, w=crop_w, h=crop_h))

        frame_idx += 1

    cap.release()
    mp_face.close()
    return windows


def _compute_crop_dims(source_w: int, source_h: int, target_w: int, target_h: int) -> tuple[int, int, int]:
    """Compute crop dimensions and scale step.

    Returns (crop_w, crop_h, scale_needed_after).
    Scale_needed = 1 if we need to resize after cropping, 0 if native match.
    """
    target_aspect = target_w / target_h
    # Crop at max size that fits source height and target aspect
    crop_h = source_h
    crop_w = int(crop_h * target_aspect)
    if crop_w > source_w:
        # Source too narrow — crop at source width, letterbox later
        crop_w = source_w
        crop_h = int(crop_w / target_aspect)
    return crop_w, crop_h, 1


def _build_sendcmd_file(
    windows: list[_CropWindow], path: Path, crop_w: int, crop_h: int,
) -> Path:
    """Write ffmpeg `sendcmd` file for animated crop x/y.

    Format:
      <time> crop x <value>;
      <time> crop y <value>;
    """
    lines: list[str] = []
    for w in windows:
        lines.append(f"{w.t_s:.3f} crop x {w.x};")
        lines.append(f"{w.t_s:.3f} crop y {w.y};")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


async def smart_reframe_cut(
    source: SourceMedia,
    moment: Moment,
    output_path: Path,
    *,
    target_width: int = 1080,
    target_height: int = 1920,
    crf: int = 20,
    preset: str = "medium",
) -> Path:
    """Cut a moment from source with face-tracked 9:16 reframe."""
    ok, msg = _check_deps()
    if not ok:
        logger.warning("Smart reframe unavailable (%s) — falling back to center crop", msg)
        from engine.cutter import cut_clip
        return await cut_clip(
            source, moment, output_path,
            target_width=target_width, target_height=target_height,
            reframe_mode="center", crf=crf, preset=preset,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    crop_w, crop_h, _ = _compute_crop_dims(source.width, source.height, target_width, target_height)

    logger.info("Tracking faces/persons for reframe: %.1fs-%.1fs (crop %dx%d)",
                moment.start_s, moment.end_s, crop_w, crop_h)

    windows = await asyncio.to_thread(
        _track_sync,
        str(source.path),
        moment.start_s,
        moment.end_s,
        source.width, source.height,
        crop_w, crop_h,
    )

    if not windows:
        logger.warning("No tracking windows — falling back to center crop")
        from engine.cutter import cut_clip
        return await cut_clip(
            source, moment, output_path,
            target_width=target_width, target_height=target_height,
            reframe_mode="center", crf=crf, preset=preset,
        )

    # Write sendcmd file
    work_dir = output_path.parent / f".reframe_{output_path.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    sendcmd_path = work_dir / "crop.cmd"
    _build_sendcmd_file(windows, sendcmd_path, crop_w, crop_h)

    start_x = windows[0].x
    start_y = windows[0].y

    # Build filter chain:
    #   sendcmd reads crop.cmd → updates crop x/y over time
    #   crop does the cut
    #   scale normalizes to target
    # Note: sendcmd paths on Windows need special escaping; we're macOS/Linux here.
    filter_chain = (
        f"sendcmd=f={sendcmd_path.as_posix()},"
        f"crop={crop_w}:{crop_h}:{start_x}:{start_y},"
        f"scale={target_width}:{target_height}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{moment.start_s:.3f}",
        "-i", str(source.path),
        "-t", f"{moment.duration_s:.3f}",
        "-vf", filter_chain,
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if source.has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ac", "2"])
    else:
        cmd.extend(["-an"])
    cmd.append(str(output_path))

    rc, out, err = await _run(*cmd, timeout=max(moment.duration_s * 10, 120))

    # Cleanup sendcmd work dir
    try:
        shutil.rmtree(work_dir)
    except OSError:
        pass

    if rc != 0:
        logger.warning("Smart reframe ffmpeg failed — falling back to center crop: %s", err[-500:])
        from engine.cutter import cut_clip
        return await cut_clip(
            source, moment, output_path,
            target_width=target_width, target_height=target_height,
            reframe_mode="center", crf=crf, preset=preset,
        )

    return output_path
