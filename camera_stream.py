#!/usr/bin/env python3
"""Tank Cam — Pi カメラを Web 配信。

実行:
  python3 ~/camera_stream.py

ブラウザでアクセス:
  http://raspberrypi.local:8000/
"""

import io
from threading import Condition
from flask import Flask, Response, render_template_string

from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput
from libcamera import Transform


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
video_config = picam2.create_video_configuration(
    main={"size": (640, 480)},
    transform=transform,
)
picam2.configure(video_config)

output = StreamingOutput()
picam2.start_recording(JpegEncoder(), FileOutput(output))


app = Flask(__name__)

HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tank Cam</title>
  <style>
    body { margin: 0; background: #0a0a0a; color: #eee; font-family: system-ui, sans-serif; text-align: center; }
    header { padding: 16px; background: #1a1a1a; border-bottom: 1px solid #333; }
    h1 { margin: 0; font-size: 18px; font-weight: 500; letter-spacing: 0.05em; }
    .status { color: #4ade80; font-size: 12px; margin-top: 4px; }
    .frame-wrapper { padding: 20px; display: flex; justify-content: center; }
    img { max-width: 100%; max-height: 80vh; border-radius: 8px; box-shadow: 0 4px 24px rgba(0,0,0,0.5); }
  </style>
</head>
<body>
  <header>
    <h1>🚗 TANK CAM</h1>
    <div class="status">● LIVE</div>
  </header>
  <div class="frame-wrapper">
    <img src="/stream.mjpg" alt="Live stream">
  </div>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML)


def generate_frames():
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
    return Response(
        generate_frames(),
        mimetype='multipart/x-mixed-replace; boundary=FRAME',
    )


if __name__ == '__main__':
    try:
        print("Tank Cam starting on http://0.0.0.0:8000/")
        app.run(host='0.0.0.0', port=8000, threaded=True)
    finally:
        picam2.stop_recording()
        print("Camera stopped.")
