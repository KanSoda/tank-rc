"""Robust push-up rep counter — TRIPLE-signal version.

Port of WakeFit's RobustRepCounter (Swift/Vision) to Python/MediaPipe,
with an ADDED signal #3 (forearm verticality) to handle low-camera-angle
scenarios where the shoulder/face are out of frame.

Three independent signals OR-combined through a single state machine:
  1. Elbow flexion angle      — requires shoulder + elbow + wrist
  2. Shoulder vertical travel — requires shoulder (or nose as fallback)
  3. Forearm verticality      — requires only elbow + wrist  ⭐ NEW
"""
import math
import time


class MPLM:
    NOSE = 0
    LEFT_SHOULDER = 11
    RIGHT_SHOULDER = 12
    LEFT_ELBOW = 13
    RIGHT_ELBOW = 14
    LEFT_WRIST = 15
    RIGHT_WRIST = 16


def _angle_deg(p_a, p_vertex, p_c):
    v1 = (p_a[0] - p_vertex[0], p_a[1] - p_vertex[1])
    v2 = (p_c[0] - p_vertex[0], p_c[1] - p_vertex[1])
    m1 = math.hypot(v1[0], v1[1])
    m2 = math.hypot(v2[0], v2[1])
    if m1 == 0 or m2 == 0:
        return None
    c = (v1[0] * v2[0] + v1[1] * v2[1]) / (m1 * m2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _landmark(landmarks, idx, min_visibility):
    lm = landmarks[idx]
    if lm.visibility < min_visibility:
        return None
    return (lm.x, lm.y)


def _forearm_angle_from_vertical(elbow, wrist):
    """Angle of the forearm vector (elbow→wrist) measured from the vertical
    axis, in degrees. Returns 0° when the forearm is perfectly vertical
    (top of a push-up) and grows toward 90° as it flattens (bottom).
    """
    dx = abs(wrist[0] - elbow[0])
    dy = abs(wrist[1] - elbow[1])
    if dx == 0 and dy == 0:
        return None
    # atan2(dx, dy): dx=opposite, dy=adjacent ⇒ angle from vertical axis
    return math.degrees(math.atan2(dx, dy))


class PushupCounter:
    """Counts push-up reps from MediaPipe Pose landmarks. Robust to partial
    occlusion via three independent signals."""

    # --- Tunable thresholds ---
    # Signal 1: elbow flexion
    UP_ANGLE = 150.0
    DOWN_ANGLE = 110.0
    # Signal 2: body vertical travel (auto-ranging)
    MIN_VERTICAL_RANGE = 0.045
    V_DOWN_RATIO = 0.62
    V_UP_RATIO = 0.30
    V_RANGE_DECAY = 0.003
    # Signal 3: forearm verticality  ⭐ NEW
    FOREARM_UP_DEG = 22.0    # ≤ this ⇒ arm extended (top)
    FOREARM_DOWN_DEG = 38.0  # ≥ this ⇒ arm bent (bottom)

    # Smoothing & timing
    SMOOTHING_ALPHA = 0.35
    DEBOUNCE_SEC = 0.45
    # Visibility (relaxed from Swift original's 0.3 for occlusion tolerance)
    MIN_VISIBILITY = 0.2

    STATE_UNKNOWN = "unknown"
    STATE_UP = "up"
    STATE_DOWN = "down"

    def __init__(self, on_rep=None):
        self.on_rep = on_rep
        self.count = 0
        self.state = self.STATE_UNKNOWN
        self.smoothed_angle = None
        self.smoothed_forearm = None
        self.last_rep_depth = None
        self.vertical_ratio = None
        self._v_min = None
        self._v_max = None
        self._current_rep_depth = 180.0
        self._last_count_ts = 0.0
        self._last_landmarks = None
        # Which signal(s) drove the last state transition (debug)
        self._signal_trigger = ""

    def reset(self):
        cb = self.on_rep
        self.__init__(on_rep=cb)

    def consume(self, landmarks, timestamp=None):
        if timestamp is None:
            timestamp = time.time()
        self._last_landmarks = landmarks

        # Pull the 6 joints we care about (each may be None if low visibility)
        ls = _landmark(landmarks, MPLM.LEFT_SHOULDER, self.MIN_VISIBILITY)
        le = _landmark(landmarks, MPLM.LEFT_ELBOW, self.MIN_VISIBILITY)
        lw = _landmark(landmarks, MPLM.LEFT_WRIST, self.MIN_VISIBILITY)
        rs = _landmark(landmarks, MPLM.RIGHT_SHOULDER, self.MIN_VISIBILITY)
        re = _landmark(landmarks, MPLM.RIGHT_ELBOW, self.MIN_VISIBILITY)
        rw = _landmark(landmarks, MPLM.RIGHT_WRIST, self.MIN_VISIBILITY)

        # ===== Signal 1: elbow flexion angle (min of left/right) =====
        angles = []
        if ls and le and lw:
            a = _angle_deg(ls, le, lw)
            if a is not None:
                angles.append(a)
        if rs and re and rw:
            a = _angle_deg(rs, re, rw)
            if a is not None:
                angles.append(a)
        raw_angle = min(angles) if angles else None

        angle_is_down = False
        angle_is_up = False
        if raw_angle is not None:
            self.smoothed_angle = (
                raw_angle if self.smoothed_angle is None
                else self.SMOOTHING_ALPHA * self.smoothed_angle
                     + (1 - self.SMOOTHING_ALPHA) * raw_angle
            )
            self._current_rep_depth = min(self._current_rep_depth, self.smoothed_angle)
            angle_is_down = self.smoothed_angle <= self.DOWN_ANGLE
            angle_is_up = self.smoothed_angle >= self.UP_ANGLE

        # ===== Signal 2: body vertical travel (auto-ranging) =====
        body_y = None
        if ls and rs:
            body_y = (ls[1] + rs[1]) / 2
        elif ls:
            body_y = ls[1]
        elif rs:
            body_y = rs[1]
        else:
            nose = _landmark(landmarks, MPLM.NOSE, self.MIN_VISIBILITY)
            if nose:
                body_y = nose[1]

        vert_is_down = False
        vert_is_up = False
        if body_y is not None:
            lo = min(self._v_min, body_y) if self._v_min is not None else body_y
            hi = max(self._v_max, body_y) if self._v_max is not None else body_y
            self._v_min = lo + self.V_RANGE_DECAY * (body_y - lo)
            self._v_max = hi - self.V_RANGE_DECAY * (hi - body_y)
            v_range = self._v_max - self._v_min
            if v_range >= self.MIN_VERTICAL_RANGE:
                ratio = (body_y - self._v_min) / v_range
                self.vertical_ratio = ratio
                vert_is_down = ratio >= self.V_DOWN_RATIO
                vert_is_up = ratio <= self.V_UP_RATIO
            else:
                self.vertical_ratio = None

        # ===== Signal 3: forearm verticality (NEW — works without shoulder) =====
        forearm_angles = []
        if le and lw:
            a = _forearm_angle_from_vertical(le, lw)
            if a is not None:
                forearm_angles.append(a)
        if re and rw:
            a = _forearm_angle_from_vertical(re, rw)
            if a is not None:
                forearm_angles.append(a)
        # Use the MIN of the two — when one arm is more vertical, body is at top
        raw_forearm = min(forearm_angles) if forearm_angles else None

        forearm_is_down = False
        forearm_is_up = False
        if raw_forearm is not None:
            self.smoothed_forearm = (
                raw_forearm if self.smoothed_forearm is None
                else self.SMOOTHING_ALPHA * self.smoothed_forearm
                     + (1 - self.SMOOTHING_ALPHA) * raw_forearm
            )
            forearm_is_up = self.smoothed_forearm <= self.FOREARM_UP_DEG
            forearm_is_down = self.smoothed_forearm >= self.FOREARM_DOWN_DEG

        # ===== Combine all three signals =====
        is_down = angle_is_down or vert_is_down or forearm_is_down
        is_up = angle_is_up or vert_is_up or forearm_is_up

        # Track which signal(s) is asserting
        triggers = []
        if is_down:
            if angle_is_down: triggers.append("ang↓")
            if vert_is_down: triggers.append("vrt↓")
            if forearm_is_down: triggers.append("fa↓")
        if is_up:
            if angle_is_up: triggers.append("ang↑")
            if vert_is_up: triggers.append("vrt↑")
            if forearm_is_up: triggers.append("fa↑")
        self._signal_trigger = "+".join(triggers) if triggers else ""

        new_state = self.state
        if is_up:
            new_state = self.STATE_UP
        elif is_down:
            new_state = self.STATE_DOWN

        if self.state == self.STATE_DOWN and new_state == self.STATE_UP:
            if timestamp - self._last_count_ts >= self.DEBOUNCE_SEC:
                self.count += 1
                self._last_count_ts = timestamp
                self.last_rep_depth = self._current_rep_depth
                if self.on_rep:
                    try:
                        self.on_rep(self.count)
                    except Exception:
                        pass
            self._current_rep_depth = 180.0

        self.state = new_state

    def snapshot(self):
        snap = {
            "count": self.count,
            "state": self.state,
            "angle": round(self.smoothed_angle, 1) if self.smoothed_angle is not None else None,
            "forearm": round(self.smoothed_forearm, 1) if self.smoothed_forearm is not None else None,
            "vertical_ratio": round(self.vertical_ratio, 3) if self.vertical_ratio is not None else None,
            "last_rep_depth": round(self.last_rep_depth, 1) if self.last_rep_depth is not None else None,
            "trigger": self._signal_trigger,
        }
        if self._last_landmarks is not None:
            try:
                snap["vis"] = {
                    "L_sh": round(float(self._last_landmarks[MPLM.LEFT_SHOULDER].visibility), 2),
                    "L_el": round(float(self._last_landmarks[MPLM.LEFT_ELBOW].visibility), 2),
                    "L_wr": round(float(self._last_landmarks[MPLM.LEFT_WRIST].visibility), 2),
                    "R_sh": round(float(self._last_landmarks[MPLM.RIGHT_SHOULDER].visibility), 2),
                    "R_el": round(float(self._last_landmarks[MPLM.RIGHT_ELBOW].visibility), 2),
                    "R_wr": round(float(self._last_landmarks[MPLM.RIGHT_WRIST].visibility), 2),
                }
            except Exception:
                pass
        return snap
