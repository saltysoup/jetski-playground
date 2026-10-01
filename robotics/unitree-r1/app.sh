#!/usr/bin/env bash
# ==============================================================================
# Unitree R1: Real-Time Multimodal Voice & Vision Assistant
# Starts Riva Speech Server, Gemma-4 Multimodal Server, and Audio Daemon in BG
#
# Usage:
#   bash app.sh                  Start all services, then the interactive assistant.
#                                Services keep running after the assistant exits, so
#                                `python3 test_vision_voice_assistant.py` can be re-run directly.
#   bash app.sh --services-only  Start all services and exit.
#   bash app.sh --stop           Stop all background services (frees GPU memory).
# ==============================================================================

set -e

MODE="${1:-full}"
SERVICES="riva_server llama-server unitree_audio_daemon unitree_head_camera_daemon brainco_hand_server unitree_hand_bridge"
READY_TIMEOUT="${READY_TIMEOUT:-180}"   # seconds to wait for model loading / CUDA warmup
HANDS_ON="${HANDS:-1}"
HAND_SERVER=/home/unitree/brainco_hand_service/bin/brainco_hand_server
HAND_BRIDGE=/home/unitree/unitree_sdk2/build/bin/unitree_hand_bridge

stop_services() {
    echo "[CLEANUP] Stopping existing background processes..."
    killall -9 $SERVICES 2>/dev/null || true
    # Remove stale sockets so clients don't try to connect to dead daemons
    rm -f /tmp/unitree_audio.sock /tmp/unitree_led.sock /tmp/unitree_head_camera.sock /tmp/unitree_hand.sock
    sleep 1
}

case "$MODE" in
    --stop)
        stop_services
        echo "[OK] All services stopped."
        exit 0
        ;;
    full|--services-only)
        ;;
    *)
        echo "Usage: $0 [--services-only|--stop]"
        exit 1
        ;;
esac

echo "============================================================"
echo "[INIT] Starting Unitree R1 Multimodal Assistant Services"
echo "============================================================"

# 1. Stop any stale background servers
stop_services

# 2. Launch Riva Speech Server (ASR + Magpie TTS) in Background
#    setsid: run services in their own session so Ctrl-C in this terminal (e.g. to quit the
#    assistant) does not reach them - riva_server/llama-server install SIGINT handlers that
#    override the ignore that background jobs normally inherit, so they would shut down.
echo "[1/4] Launching Riva Speech Server (ASR + Magpie TTS)..."
export LD_LIBRARY_PATH=/home/unitree/NeMo-Speech.cpp/build-cuda/bin:$LD_LIBRARY_PATH
setsid nohup /home/unitree/NeMo-Speech.cpp/build-cuda/bin/riva_server \
  --asr.model.path /home/unitree/robot_assets/models/nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --tts.magpie-model /home/unitree/robot_assets/models/magpie_tts_multilingual_357m.v2602.f16.gguf \
  --tts.codec-model /home/unitree/robot_assets/models/nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf \
  --tts.tokenizer-model-dir /home/unitree/robot_assets/models/magpie-tts/extracted \
  --bind 127.0.0.1:50051 > /home/unitree/riva_server.log 2>&1 &

# 3. Launch Native CUDA Gemma-4 Multimodal Server in Background
echo "[2/4] Launching Gemma-4 Multimodal VLM Server (Port 8000)..."
export LD_LIBRARY_PATH=/home/unitree/NeMo-Speech.cpp/llama.cpp/build-cuda/bin:$LD_LIBRARY_PATH
setsid nohup /home/unitree/NeMo-Speech.cpp/llama.cpp/build-cuda/bin/llama-server \
  -m /home/unitree/robot_assets/models/gemma-4-E2B-it-q8_0.gguf \
  --mmproj /home/unitree/robot_assets/models/mmproj-gemma-4-E2B-f16.gguf \
  --host 127.0.0.1 \
  --port 8000 \
  -c 2048 \
  -ngl 99 \
  -t 6 \
  -ub 1024 \
  -b 1024 \
  --flash-attn on \
  --cache-ram 2048 \
  --reasoning off > /home/unitree/llama_server.log 2>&1 &

# 4. Launch Persistent Gapless Audio Daemon & Head Camera Daemon
echo "[3/4] Launching Persistent Unitree Audio Daemon..."
setsid nohup /home/unitree/unitree_sdk2/build/bin/unitree_audio_daemon eth10 > /home/unitree/audio_daemon.log 2>&1 &

echo "[4/4] Launching Persistent Unitree Head Eye Camera Daemon (DDS eth10)..."
setsid nohup /home/unitree/unitree_sdk2/build/bin/unitree_head_camera_daemon eth10 > /home/unitree/head_camera_daemon.log 2>&1 &

# Hands (BrainCo Revo2): hand server (serial <-> DDS) + bridge (unix socket <-> DDS). HANDS=0 to skip.
if [ "$HANDS_ON" = "1" ]; then
    if [ -x "$HAND_SERVER" ] && [ -x "$HAND_BRIDGE" ]; then
        echo "[+] Launching BrainCo hand server + hand bridge (NOTE: both hands OPEN when the server starts)..."
        (cd "$(dirname "$HAND_SERVER")" && setsid nohup "$HAND_SERVER" --network_interface eth10 \
            > /home/unitree/brainco_hand.log 2>&1 < /dev/null &)
        setsid nohup "$HAND_BRIDGE" eth10 > /home/unitree/hand_bridge.log 2>&1 < /dev/null &
    else
        echo "[WARN] Hands skipped: $HAND_SERVER or $HAND_BRIDGE missing (see README: Hands)."
        HANDS_ON=0
    fi
fi

# 5. Wait for GPU memory initialization & server readiness
#    Riva: gRPC port accepting connections. llama-server: /health returns 200 only after the
#    model is loaded (the port opens earlier and answers 503 while loading).
echo "[WAIT] Waiting for Riva (50051) & Gemma-4 (8000/health) to finish loading (timeout ${READY_TIMEOUT}s)..."
WAITED=0
READY=0
while [ $WAITED -lt $READY_TIMEOUT ]; do
    for svc in riva_server llama-server; do
        if ! pgrep -x "$svc" > /dev/null; then
            echo "[FATAL] $svc exited during startup. Last log lines:"
            tail -n 20 "/home/unitree/$( [ "$svc" = "riva_server" ] && echo riva_server || echo llama_server ).log"
            exit 1
        fi
    done
    if nc -z 127.0.0.1 50051 2>/dev/null && curl -sf http://127.0.0.1:8000/health > /dev/null 2>&1; then
        READY=1
        echo "[OK] All neural services are fully online and ready! (${WAITED}s)"
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

if [ $READY -ne 1 ]; then
    echo "[WARN] Services not ready after ${READY_TIMEOUT}s - check /home/unitree/riva_server.log and /home/unitree/llama_server.log"
fi

DAEMONS="unitree_audio_daemon unitree_head_camera_daemon"
[ "$HANDS_ON" = "1" ] && DAEMONS="$DAEMONS brainco_hand_server unitree_hand_bridge"
for daemon in $DAEMONS; do
    if ! pgrep -f "bin/$daemon" > /dev/null; then
        echo "[WARN] $daemon is not running - see its log in /home/unitree/"
    fi
done

echo "============================================================"
echo "[STATUS] Active Background Services:"
ps aux | grep -E '(riva_server|llama-server|unitree_audio_daemon|unitree_head_camera_daemon|brainco_hand_server|unitree_hand_bridge)' | grep -v grep || true
echo "============================================================"

if [ "$MODE" = "--services-only" ]; then
    echo "[READY] Services running. Start the assistant with: python3 /home/unitree/test_vision_voice_assistant.py"
    echo "[READY] Stop services with: bash $0 --stop"
    exit 0
fi

# 6. Launch Interactive Voice & Vision Assistant
echo "[READY] Launching Jason Interactive Assistant..."
echo "[INFO] Services keep running after exit. Stop them with: bash $0 --stop"
export CAMERA_SOURCE="${CAMERA_SOURCE:-head}"
python3 /home/unitree/test_vision_voice_assistant.py
