# Tank RC

Browser-based remote control UI for a Raspberry Pi tank robot (Freenove FNK0077).

## Features

- Live MJPEG camera feed
- Joystick + on-screen action buttons
- Keyboard controls (Arrow keys / WASD, Space to stop)
- Speed slider, emergency stop

## Local preview

```
open index.html
```

## Backend

The UI expects a Pi running `camera_stream.py` (Flask + Picamera2) on `http://raspberrypi.local:8000/`.
WebSocket control integration is WIP.
