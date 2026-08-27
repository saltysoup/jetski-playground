#!/usr/bin/env bash
# ==============================================================================
# Unitree R1: Real-Time Multimodal Voice & Vision Assistant
# Starts Riva Speech Server, Gemma-4 Multimodal Server, and Audio Daemon in BG
# ==============================================================================

set -e

echo "============================================================"
echo "[INIT] Starting Unitree R1 Multimodal Assistant Services"
echo "============================================================"

# 1. Stop any stale background servers
echo "[CLEANUP] Stopping existing background processes..."
killall -9 riva_server llama-server unitree_audio_daemon 2>/dev/null || true
sleep 1

# 2. Launch Riva Speech Server (ASR + Magpie TTS) in Background
echo "[1/3] Launching Riva Speech Server (ASR + Magpie TTS)..."
export LD_LIBRARY_PATH=/home/unitree/NeMo-Speech.cpp/build-cuda/bin:$LD_LIBRARY_PATH
nohup /home/unitree/NeMo-Speech.cpp/build-cuda/bin/riva_server \
  --asr.model.path /home/unitree/robot_assets/models/nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --tts.magpie-model /home/unitree/robot_assets/models/magpie_tts_multilingual_357m.v2602.f16.gguf \
  --tts.codec-model /home/unitree/robot_assets/models/nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf \
  --tts.tokenizer-model-dir /home/unitree/robot_assets/models/magpie-tts/extracted \
  --bind 127.0.0.1:50051 > /home/unitree/riva_server.log 2>&1 &

# 3. Launch Native CUDA Gemma-4 Multimodal Server in Background
echo "[2/3] Launching Gemma-4 Multimodal VLM Server (Port 8000)..."
export LD_LIBRARY_PATH=/home/unitree/NeMo-Speech.cpp/llama.cpp/build-cuda/bin:$LD_LIBRARY_PATH
nohup /home/unitree/NeMo-Speech.cpp/llama.cpp/build-cuda/bin/llama-server \
  -m /home/unitree/robot_assets/models/gemma-4-E2B-it-q8_0.gguf \
  --mmproj /home/unitree/robot_assets/models/mmproj-gemma-4-E2B-f16.gguf \
  --host 127.0.0.1 \
  --port 8000 \
  -c 2048 \
  -ngl 99 \
  -t 6 \
  -ub 512 \
  -b 512 \
  --flash-attn on \
  --cache-ram 0 \
  --reasoning off > /home/unitree/llama_server.log 2>&1 &

# 4. Launch Persistent Gapless Audio Daemon
echo "[3/3] Launching Persistent Unitree Audio Daemon..."
nohup /home/unitree/unitree_sdk2/build/bin/unitree_audio_daemon eth10 > /home/unitree/audio_daemon.log 2>&1 &

# 5. Wait for GPU memory initialization
echo "[WAIT] Warming up neural engines (5 seconds)..."
sleep 5

echo "============================================================"
echo "[STATUS] Active Background Services:"
ps aux | grep -E '(riva_server|llama-server|unitree_audio_daemon)' | grep -v grep
echo "============================================================"

# 6. Launch Interactive Voice & Vision Assistant
echo "[READY] Launching Jason Interactive Assistant..."
python /home/unitree/test_vision_voice_assistant.py
