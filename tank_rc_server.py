#!/usr/bin/env python3
"""Tank RC Server — Pi バックエンド統合サーバ。

エンドポイント:
  GET  /                  → UI (index.html)
  GET  /stream.mjpg       → MJPEG ライブ配信
  GET  /snapshot.jpg      → 1枚スナップショット
  POST /move?left=L&right=R → モーター制御 (-2000 ~ 2000)
  POST /stop              → 緊急停止
  POST /curtain/open      → カーテン開
  POST /curtain/close     → カーテン閉
  POST /led/on, /led/off  → LED 制御
  GET  /stats             → Pi 状態 (CPU温度等)

実行:
  python3 ~/tank_rc_server.py
"""

import io
import os
import sys
import time
import threading
from threading import Condition

from flask import Flask, Response, request, jsonify

sys.path.insert(0, '/home/pi/Freenove_Tank_Robot_Kit_for_Raspberry_Pi/Code/Server')
sys.path.insert(0, '/home/pi')  # for workout.py

from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput
from libcamera import Transform

# Lazy MediaPipe + OpenCV imports (slow)
try:
    from workout import PushupCounter
except ImportError:
    PushupCounter = None


# ===== Camera =====
class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()
    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


picam2 = Picamera2()
transform = Transform(hflip=1, vflip=1)
video_config = picam2.create_video_configuration(main={"size": (640, 480)}, transform=transform)
picam2.configure(video_config)
output = StreamingOutput()
picam2.start_recording(JpegEncoder(), FileOutput(output))


# ===== Motor (lazy import — needs board power) =====
_motor = None
_motor_lock = threading.Lock()
_last_cmd_time = 0
_last_speeds = (0, 0)  # (left, right) for kickstart detection
SAFETY_TIMEOUT = 0.5
KICK_SPEED = 2000      # キックスタート時のフルパワー
KICK_DURATION = 0.08   # 80ms 程度のバースト

def get_motor():
    global _motor
    if _motor is None:
        from motor import tankMotor
        _motor = tankMotor()
    return _motor


def needs_kick(prev, curr):
    """前回0 → 今回非0、または符号反転 ならキック必要"""
    if curr == 0:
        return False
    if prev == 0:
        return True
    if (prev > 0) != (curr > 0):
        return True
    return False


def kick_value(prev, curr):
    """キックが必要な側はフルパワー、不要な側は目標値そのまま"""
    if needs_kick(prev, curr):
        return KICK_SPEED if curr > 0 else -KICK_SPEED
    return curr


def safety_watchdog():
    """モーターコマンドが SAFETY_TIMEOUT 秒来なければ自動停止。"""
    global _last_speeds
    while True:
        time.sleep(0.1)
        if _last_cmd_time > 0 and (time.time() - _last_cmd_time) > SAFETY_TIMEOUT:
            try:
                with _motor_lock:
                    if _motor is not None:
                        _motor.setMotorModel(0, 0)
                        _last_speeds = (0, 0)
            except Exception:
                pass


threading.Thread(target=safety_watchdog, daemon=True).start()


# ===== LED (lazy) =====
_led = None
_led_state = False

def get_led():
    global _led
    if _led is None:
        from led import Led
        _led = Led()
    return _led


# ===== Flask =====
app = Flask(__name__)
INDEX_PATH = '/home/pi/tank_rc_index.html'


@app.after_request
def cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp


@app.route('/', methods=['GET'])
def index():
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH, 'r', encoding='utf-8') as f:
            return f.read()
    return '<h1>Tank RC</h1><p>index.html not found. Place it at ' + INDEX_PATH + '</p>'


def gen_frames():
    while True:
        with output.condition:
            output.condition.wait()
            frame = output.frame
        yield (b'--FRAME\r\n'
               b'Content-Type: image/jpeg\r\n'
               b'Content-Length: ' + str(len(frame)).encode() + b'\r\n\r\n'
               + frame + b'\r\n')


@app.route('/stream.mjpg')
def stream():
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=FRAME')


@app.route('/snapshot.jpg')
def snapshot():
    with output.condition:
        output.condition.wait()
        frame = output.frame
    resp = Response(frame, mimetype='image/jpeg')
    resp.headers['Content-Disposition'] = 'inline; filename="snapshot.jpg"'
    return resp


@app.route('/move', methods=['POST', 'GET'])
def move():
    global _last_cmd_time, _last_speeds
    try:
        left = max(-2000, min(2000, int(request.values.get('left', 0))))
        right = max(-2000, min(2000, int(request.values.get('right', 0))))
        prev_l, prev_r = _last_speeds
        kicked = False
        with _motor_lock:
            m = get_motor()
            if needs_kick(prev_l, left) or needs_kick(prev_r, right):
                # 衝突後の過電流保護ラッチを解除するための事前 0 信号
                # (前回の停止状態で driver IC がフォルト保持していた場合の復旧)
                m.setMotorModel(0, 0)
                time.sleep(0.04)
                # キックスタート (起動電流確保)
                kl = kick_value(prev_l, left)
                kr = kick_value(prev_r, right)
                m.setMotorModel(kl, kr)
                time.sleep(KICK_DURATION)
                kicked = True
            m.setMotorModel(left, right)
            _last_speeds = (left, right)
        _last_cmd_time = time.time()
        return jsonify(ok=True, left=left, right=right, kicked=kicked)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route('/stop', methods=['POST', 'GET'])
def stop():
    global _last_cmd_time, _last_speeds
    try:
        with _motor_lock:
            get_motor().setMotorModel(0, 0)
            _last_speeds = (0, 0)
        _last_cmd_time = 0
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route('/reset', methods=['POST', 'GET'])
def reset_motors():
    """ドライバ IC のフォルトラッチを強制解除する。
    過電流保護(障害物に衝突して停止後など)で動かなくなった時に呼ぶ。
    複数回の 0 パルスで確実にフォルト解除。"""
    global _last_cmd_time, _last_speeds
    try:
        with _motor_lock:
            m = get_motor()
            # 3回の 0 パルス + 各 60ms 待機 で完全に駆動 IC をリセット
            for _ in range(3):
                m.setMotorModel(0, 0)
                time.sleep(0.06)
            _last_speeds = (0, 0)
        _last_cmd_time = 0
        return jsonify(ok=True, message='Motor driver reset complete')
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route('/curtain/<action>', methods=['POST', 'GET'])
def curtain(action):
    global _last_cmd_time
    if action not in ('open', 'close'):
        return jsonify(ok=False, error='unknown action'), 400
    duration = float(request.values.get('duration', 3.75))
    speed = int(request.values.get('speed', 800))
    sign = 1 if action == 'open' else -1

    def run():
        global _last_cmd_time
        try:
            m = get_motor()
            with _motor_lock:
                m.setMotorModel(sign * speed, sign * speed)
            end = time.time() + duration
            while time.time() < end:
                _last_cmd_time = time.time()
                time.sleep(0.1)
        finally:
            try:
                with _motor_lock:
                    m.setMotorModel(0, 0)
            except Exception:
                pass
            _last_cmd_time = 0

    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True, action=action, duration=duration, speed=speed)


@app.route('/led/<state>', methods=['POST', 'GET'])
def led_route(state):
    global _led_state
    try:
        led = get_led()
        if state == 'on':
            led.colorWipe((255, 100, 0))
            _led_state = True
        elif state == 'off':
            led.colorWipe((0, 0, 0))
            _led_state = False
        elif state == 'toggle':
            if _led_state:
                led.colorWipe((0, 0, 0))
                _led_state = False
            else:
                led.colorWipe((255, 100, 0))
                _led_state = True
        else:
            return jsonify(ok=False, error='unknown state'), 400
        return jsonify(ok=True, state=_led_state)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route('/stats')
def stats():
    cpu_temp = None
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            cpu_temp = int(f.read().strip()) / 1000.0
    except Exception:
        pass
    return jsonify(ok=True, cpu_temp=cpu_temp, led=_led_state)


# ============ WORKOUT (push-up detection) ============
_workout_active = False
_workout_target = 10
_workout_counter = None
_workout_fps = 0.0
_pose_detector = None
_cv2 = None
_np = None


def _get_pose():
    global _pose_detector, _cv2, _np
    if _pose_detector is None:
        import cv2 as cv2_mod
        import numpy as np_mod
        import mediapipe as mp
        _cv2 = cv2_mod
        _np = np_mod
        _pose_detector = mp.solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=0,  # 0 = Lite (fast on Pi 4)
            smooth_landmarks=True,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
    return _pose_detector


def _rep_dance(rep_num):
    """各レップ検出時の小さな前後ウィグル(可愛い)"""
    def dance():
        try:
            with _motor_lock:
                m = get_motor()
                m.setMotorModel(1200, 1200)
                time.sleep(0.18)
                m.setMotorModel(-1200, -1200)
                time.sleep(0.18)
                m.setMotorModel(0, 0)
        except Exception:
            pass
    threading.Thread(target=dance, daemon=True).start()


def _completion_dance():
    """10レップ達成時のお祝いダンス(スピン)"""
    def celebrate():
        try:
            with _motor_lock:
                m = get_motor()
                m.setMotorModel(1500, -1500)
                time.sleep(0.7)
                m.setMotorModel(-1500, 1500)
                time.sleep(0.7)
                m.setMotorModel(1200, 1200)
                time.sleep(0.2)
                m.setMotorModel(-1200, -1200)
                time.sleep(0.2)
                m.setMotorModel(0, 0)
        except Exception:
            pass
    threading.Thread(target=celebrate, daemon=True).start()


def _workout_loop():
    """カメラフレームを処理して腕立て伏せをカウントする背景スレッド"""
    global _workout_active, _workout_fps
    last_frame_t = time.time()
    while True:
        if not _workout_active or _workout_counter is None:
            time.sleep(0.2)
            continue
        with output.condition:
            output.condition.wait()
            jpeg = output.frame
        if jpeg is None:
            continue
        try:
            pose = _get_pose()
            img = _cv2.imdecode(_np.frombuffer(jpeg, _np.uint8), _cv2.IMREAD_COLOR)
            if img is None:
                continue
            # 解像度落として高速化 (320幅で十分)
            h, w = img.shape[:2]
            if w > 320:
                scale = 320.0 / w
                img = _cv2.resize(img, (int(w * scale), int(h * scale)))
            rgb = _cv2.cvtColor(img, _cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            result = pose.process(rgb)
            if result.pose_landmarks:
                _workout_counter.consume(result.pose_landmarks.landmark)
                if (_workout_counter.count >= _workout_target
                        and _workout_active):
                    _workout_active = False
                    _completion_dance()
            now = time.time()
            dt = now - last_frame_t
            if dt > 0:
                _workout_fps = 0.7 * _workout_fps + 0.3 * (1.0 / dt)
            last_frame_t = now
        except Exception as e:
            print(f"workout_loop error: {e}")
            time.sleep(0.2)


threading.Thread(target=_workout_loop, daemon=True).start()


@app.route('/workout/start', methods=['POST', 'GET'])
def workout_start():
    global _workout_active, _workout_counter, _workout_target
    if PushupCounter is None:
        return jsonify(ok=False, error='workout.py module not available'), 500
    _workout_target = int(request.values.get('target', 10))
    _workout_counter = PushupCounter(on_rep=_rep_dance)
    _workout_active = True
    return jsonify(ok=True, target=_workout_target)


@app.route('/workout/stop', methods=['POST', 'GET'])
def workout_stop():
    global _workout_active
    _workout_active = False
    count = _workout_counter.count if _workout_counter else 0
    return jsonify(ok=True, count=count)


@app.route('/workout/reset', methods=['POST', 'GET'])
def workout_reset():
    if _workout_counter:
        _workout_counter.reset()
    return jsonify(ok=True)


@app.route('/workout/status')
def workout_status():
    base = {
        'active': _workout_active,
        'target': _workout_target,
        'fps': round(_workout_fps, 1),
    }
    if _workout_counter is None:
        base.update({'count': 0, 'state': 'idle', 'angle': None, 'vertical_ratio': None})
        return jsonify(base)
    base.update(_workout_counter.snapshot())
    return jsonify(base)


@app.route('/workout')
def workout_page():
    path = '/home/pi/tank_rc_workout.html'
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    return '<h1>workout.html not found</h1>'


if __name__ == '__main__':
    try:
        print("Tank RC Server starting on http://0.0.0.0:8000/")
        app.run(host='0.0.0.0', port=8000, threaded=True)
    finally:
        picam2.stop_recording()
        print("Camera stopped.")
