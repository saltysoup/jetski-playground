#!/usr/bin/env python3
"""
Unitree R1 Test 1: Audio Loopback (Mic & Speaker)
"""

import time
import os
import numpy as np
import sounddevice as sd
import soundfile as sf

def speak_tts_prompt(text="Tell me something"):
    print(f"🔊 Robot saying: \"{text}\"")
    os.system(f'espeak "{text}" 2>/dev/null || aplay /usr/share/sounds/alsa/Front_Center.wav 2>/dev/null')

def record_audio(duration_sec=5, sample_rate=16000):
    print(f"\n🎙️ RECORDING for {duration_sec} seconds... Speak into the robot mic now!")
    audio = sd.rec(int(duration_sec * sample_rate), samplerate=sample_rate, channels=1, dtype='int16')
    sd.wait()
    print("🛑 Recording finished!")
    return audio

def playback_audio(audio_data, sample_rate=16000):
    print("🔊 Playing back recorded audio through onboard speakers...")
    sd.play(audio_data, samplerate=sample_rate)
    sd.wait()
    print("✅ Playback complete!")

def main():
    print("=" * 60)
    print("🤖 Unitree R1 Audio Test: Microphone & Speaker Loopback")
    print("=" * 60)
    
    # 1. Robot prompts user
    speak_tts_prompt("Tell me something")
    
    # 2. Wait 1 second
    print("⏳ Waiting 1 second...")
    time.sleep(1.0)
    
    # 3. Record for 5 seconds
    audio_data = record_audio(duration_sec=5, sample_rate=16000)
    
    # 4. Playback
    playback_audio(audio_data, sample_rate=16000)

if __name__ == "__main__":
    main()
