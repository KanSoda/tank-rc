"""Robust push-up rep counter.

Port of WakeFit's RobustRepCounter (Swift/Vision) to Python/MediaPipe.

Dual-signal design — counts on whichever signal cleanly completes a down→up cycle:
  1. Elbow flexion angle (absolute thresholds, no calibration needed)
  2. Shoulder vertical travel (auto-ranging, rescues reps when elbow occluded)

Coordinate convention: MediaPipe normalized (x: 0=left→1=right, y: 0=top→1=bottom)
which is opposite of Apple Vision (y: 0=bottom→1=top), so the vertical
direction logic is inverted from the Swift original.
"""
import math
import time


# MediaPipe Pose landmark indices (33 landmarks total)
class MPLM:
    NOSE = 0
    LEFT_SHOULDER = 11
    RIGHT_SHOULDER = 12
    LEFT_ELBOW = 13
    RIGHT_ELBOW = 14
    LEFT_WRIST = 15
    RIGHT_WRIST = 16


def _angle_deg(p_a, p_vertex, p_c):
    """Angle at p_vertex formed by rays vertex→a, vertex→c, in degrees (0..180)."""
    v1 = (p_a[0] - p_vertex[0], p_a[1] - p_vertex[1])
    v2 = (p_c[0] - p_vertex[0], p_c[1] - p_vertex[1])
    m1 = math.hypot(v1[0], v1[1])
    m2 = math.hypot(v2[0], v2[1])
    if m1 == 0 or m2 == 0:
        return None
    cosine = (v1[0] * v2[0] + v1[1] * v2[1]) / (m1 * m2)
    cosine = max(-1.0, min(1.0, cosine))
    return math.degrees(math.acos(cosine))


def _landmark(landmarks, idx, min_visibility):
    lm = landmarks[idx]
    if lm.visibility < min_visibility:
        return None
    return (lm.x, lm.y)


class PushupCounter:
    """Counts push-up reps from MediaPipe Pose landmarks."""

    # Tunables (ported verbatim from RobustRepCounter.makePushupCounter)
    UP_ANGLE = 150.0            # elbow ≥ this ⇒ arms extended (top)
    DOWN_ANGLE = 110.0          # elbow ≤ this ⇒ arms bent (bottom)
    SMOOTHING_ALPHA = 0.35      # low-pass filter on angle
    MIN_VERTICAL_RANGE = 0.045  # minimum Y travel before vertical signal trusted
    V_DOWN_RATIO = 0.62         # vertical ratio ≥ this ⇒ "down"
    V_UP_RATIO = 0.30           # vertical ratio ≤ this ⇒ "up"
    V_RANGE_DECAY = 0.003       # how fast stale extremes decay toward current
    DEBOUNCE_SEC = 0.45         # min seconds between counted reps
    MIN_VISIBILITY = 0.3        # landmark visibility threshold

    STATE_UNKNOWN = "unknown"
    STATE_UP = "up"
    STATE_DOWN = "down"

    def __init__(self, on_rep=None):
        self.on_rep = on_rep
        self.count = 0
        self.state = self.STATE_UNKNOWN
        self.smoothed_angle = None
        self.last_rep_depth = None
        self.vertical_ratio = None
        self._v_min = None
        self._v_max = None
        self._current_rep_depth = 180.0
        self._last_count_ts = 0.0
        self._last_landmarks = None

    def reset(self):
        cb = self.on_rep
        self.__init__(on_rep=cb)

    def consume(self, landmarks, timestamp=None):
        """Process one pose frame. `landmarks` = MediaPipe Pose .landmark list (33 items)."""
        if timestamp is None:
            timestamp = time.time()
        self._last_landmarks = landmarks

        # --- Signal 1: elbow flexion (min of left/right) ---
        ls = _landmark(landmarks, MPLM.LEFT_SHOULDER, self.MIN_VISIBILITY)
        le = _landmark(landmarks, MPLM.LEFT_ELBOW, self.MIN_VISIBILITY)
        lw = _landmark(landmarks, MPLM.LEFT_WRIST, self.MIN_VISIBILITY)
        rs = _landmark(landmarks, MPLM.RIGHT_SHOULDER, self.MIN_VISIBILITY)
        re = _landmark(landmarks, MPLM.RIGHT_ELBOW, self.MIN_VISIBILITY)
        rw = _landmark(landmarks, MPLM.RIGHT_WRIST, self.MIN_VISIBILITY)

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
            if self.smoothed_angle is None:
                self.smoothed_angle = raw_angle
            else:
                self.smoothed_angle = (
                    self.SMOOTHING_ALPHA * self.smoothed_angle
                    + (1 - self.SMOOTHING_ALPHA) * raw_angle
                )
            self._current_rep_depth = min(self._current_rep_depth, self.smoothed_angle)
            angle_is_down = self.smoothed_angle <= self.DOWN_ANGLE
            angle_is_up = self.smoothed_angle >= self.UP_ANGLE

        # --- Signal 2: shoulder/nose vertical travel (auto-ranging) ---
        # MediaPipe Y: 0 = top of frame, 1 = bottom
        # Push-up bottom = body close to floor = larger Y
        # Push-up top    = body further from floor = smaller Y
        body_y = None
        if ls and rs:
            body_y = (ls[1] + rs[1]) / 2
        else:
            nose = _landmark(landmarks, MPLM.NOSE, self.MIN_VISIBILITY)
            if nose:
                body_y = nose[1]

        vert_is_down = False
        vert_is_up = False
        if body_y is not None:
            lo = min(self._v_min, body_y) if self._v_min is not None else body_y
            hi = max(self._v_max, body_y) if self._v_max is not None else body_y
            # Decay stale extremes toward current
            self._v_min = lo + self.V_RANGE_DECAY * (body_y - lo)
            self._v_max = hi - self.V_RANGE_DECAY * (hi - body_y)
            v_range = self._v_max - self._v_min
            if v_range >= self.MIN_VERTICAL_RANGE:
                # ratio: 0 at top of observed travel (small Y), 1 at bottom (large Y)
                ratio = (body_y - self._v_min) / v_range
                self.vertical_ratio = ratio
                vert_is_down = ratio >= self.V_DOWN_RATIO
                vert_is_up = ratio <= self.V_UP_RATIO
            else:
                self.vertical_ratio = None

        # --- State machine ---
        is_down = angle_is_down or vert_is_down
        is_up = angle_is_up or vert_is_up

        new_state = self.state
        if is_up:
            new_state = self.STATE_UP
        elif is_down:
            new_state = self.STATE_DOWN

        # Count on down → up transition (with debounce)
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
        """Return debug info for UI overlay."""
        snap = {
            "count": self.count,
            "state": self.state,
            "angle": round(self.smoothed_angle, 1) if self.smoothed_angle is not None else None,
            "vertical_ratio": round(self.vertical_ratio, 3) if self.vertical_ratio is not None else None,
            "last_rep_depth": round(self.last_rep_depth, 1) if self.last_rep_depth is not None else None,
        }
        if self._last_landmarks is not None:
            try:
                le = self._last_landmarks[MPLM.LEFT_ELBOW]
                re = self._last_landmarks[MPLM.RIGHT_ELBOW]
                snap["left_elbow_visibility"] = round(float(le.visibility), 2)
                snap["right_elbow_visibility"] = round(float(re.visibility), 2)
            except Exception:
                pass
        return snap
