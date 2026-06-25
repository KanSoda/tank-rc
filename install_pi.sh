#!/bin/bash
# Tank RC — Mac side helper
# Pi が起動したらこのスクリプトを実行: ./install_pi.sh
# やること:
#   1. Pi 接続確認 (最大60秒待つ)
#   2. UI / サーバ / systemd unit を Pi に転送
#   3. systemd 有効化 → 自動起動 + 自動再起動を設定
#   4. 動作確認

set -e
PI=pi@raspberrypi.local
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== [1/4] Pi 接続待ち (最大 60秒) ==="
for i in $(seq 1 30); do
  if ssh -o ConnectTimeout=2 -o BatchMode=yes "$PI" 'echo OK' 2>/dev/null | grep -q OK; then
    echo "  Pi reachable on attempt $i"
    break
  fi
  echo "  [$i/30] waiting..."
  sleep 2
done
ssh -o ConnectTimeout=3 "$PI" 'echo connected' || { echo "ERROR: Pi unreachable. 電源 ON してから再実行を。"; exit 1; }

echo "=== [2/4] ファイル転送 ==="
scp "$REPO_DIR/index.html"          "$PI:~/tank_rc_index.html"
scp "$REPO_DIR/tank_rc_server.py"   "$PI:~/tank_rc_server.py"
scp "$REPO_DIR/tank-rc.service"     "$PI:/tmp/tank-rc.service"

echo "=== [3/4] systemd 有効化 ==="
ssh "$PI" "pkill -f tank_rc_server.py 2>/dev/null; sleep 1; \
  sudo mv /tmp/tank-rc.service /etc/systemd/system/tank-rc.service && \
  sudo systemctl daemon-reload && \
  sudo systemctl enable tank-rc.service && \
  sudo systemctl restart tank-rc.service && \
  sleep 3 && \
  sudo systemctl is-active tank-rc.service"

echo "=== [4/4] 動作確認 ==="
sleep 2
curl -s --max-time 5 http://raspberrypi.local:8000/stats && echo
echo
echo "完了!以下で確認できます:"
echo "  ブラウザ:    http://raspberrypi.local:8000/"
echo "  Vercel:     https://tank-rc.vercel.app"
echo "  ステータス:  ssh $PI 'sudo systemctl status tank-rc'"
