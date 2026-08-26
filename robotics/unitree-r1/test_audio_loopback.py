#!/usr/bin/env python3
"""
Unitree R1 Test 1: Full Unitree Audio Loopback (Mic & Speaker)
- Records via Unitree UDP Multicast Stream (239.168.123.161:5555)
- Plays back via Unitree DDS AudioClient (unitree_play_wav)
"""

import time
import os
import sys
import socket
import struct
import subprocess
import soundfile as sf
import numpy as np

# Network & Audio Config
MCAST_GRP = "239.168.123.161"
MCAST_PORT = 5555
NET_INTERFACE_IP = "192.168.123.164"
NET_INTERFACE_NAME = "eth10"
PLAYER_BIN = "/home/unitree/unitree_sdk2/build/bin/unitree_play_wav"
RECORD_WAV = "/home/unitree/record.wav"

def record_multicast_audio(duration_sec=5, sample_rate=16000):
    """Captures live audio from Unitree onboard mic via UDP Multicast."""
    print(f"\n🎙️ RECORDING for {duration_sec} seconds... Speak into the robot mic now!")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", MCAST_PORT))
    
    # Join Multicast group on eth10
    mreq = socket.inet_aton(MCAST_GRP) + socket.inet_aton(NET_INTERFACE_IP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(2.0)
    
    total_bytes_needed = sample_rate * 2 * duration_sec  # 16-bit mono
    audio_bytes = bytearray()
    
    # Flush any initial buffered packets
    time.sleep(0.1)
    while len(audio_bytes) < total_bytes_needed:
        try:
            data, _ = sock.recvfrom(4096)
            if data:
                audio_bytes.extend(data)
        except socket.timeout:
            break
            
    sock.close()
    print("🛑 Recording finished!")
    
    # Trim to exact length
    audio_bytes = audio_bytes[:total_bytes_needed]
    
    # Save to WAV file for playback
    audio_np = np.frombuffer(audio_bytes, dtype=np.int16)
    sf.write(RECORD_WAV, audio_np, sample_rate, format='WAV', subtype='PCM_16')
    return audio_bytes

def play_audio_via_unitree(wav_path=RECORD_WAV):
    """Plays WAV file through Unitree robot speakers using DDS AudioClient."""
    print("🔊 Playing back recorded audio through onboard robot speakers...")
    if os.path.exists(PLAYER_BIN) and os.path.exists(wav_path):
        subprocess.run([PLAYER_BIN, wav_path, NET_INTERFACE_NAME], check=True)
        print("✅ Playback complete!")
    else:
        print(f"❌ Player binary not found at {PLAYER_BIN}")

def main():
    print("=" * 60)
    print("🤖 Unitree R1 Audio Test: Microphone & Speaker Loopback")
    print("=" * 60)
    
    print("🔊 Robot prompt: 'Tell me something'")
    print("⏳ Waiting 1 second...")
    time.sleep(1.0)
    
    # 1. Record 5s of audio
    record_multicast_audio(duration_sec=5, sample_rate=16000)
    
    # 2. Playback
    play_audio_via_unitree(RECORD_WAV)

if __name__ == "__main__":
    main()
