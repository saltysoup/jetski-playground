#include <iostream>
#include <fstream>
#include <vector>
#include <algorithm>
#include <cmath>
#include <unitree/common/time/time_tool.hpp>
#include <unitree/robot/g1/audio/g1_audio_client.hpp>
#include "wav.hpp"

#define CHUNK_SIZE 96000  // 3 seconds

int main(int argc, char const *argv[]) {
  if (argc < 3) {
    std::cout << "Usage: unitree_play_wav <wav_path> <NetWorkInterface>" << std::endl;
    return 1;
  }

  const char* wav_path = argv[1];
  const char* net_interface = argv[2];

  unitree::robot::ChannelFactory::Instance()->Init(0, net_interface);
  unitree::robot::g1::AudioClient client;
  client.Init();
  client.SetTimeout(5.0f);

  // Set hardware speaker volume to 100%
  client.SetVolume(100);

  int32_t sample_rate = -1;
  int8_t num_channels = 0;
  bool filestate = false;
  std::vector<uint8_t> pcm = ReadWave(wav_path, &sample_rate, &num_channels, &filestate);

  if (!filestate || sample_rate != 16000 || num_channels != 1) {
    std::cerr << "Error: Only 16kHz mono WAV supported!" << std::endl;
    return 1;
  }

  size_t total_size = pcm.size();
  size_t offset = 0;
  std::string stream_id = std::to_string(unitree::common::GetCurrentTimeMillisecond());
  double duration_sec = static_cast<double>(total_size) / (16000.0 * 2.0);

  while (offset < total_size) {
    size_t remaining = total_size - offset;
    size_t current_chunk_size = std::min(static_cast<size_t>(CHUNK_SIZE), remaining);
    std::vector<uint8_t> chunk(pcm.begin() + offset, pcm.begin() + offset + current_chunk_size);
    client.PlayStream("tts_output", stream_id, chunk);
    offset += current_chunk_size;
    unitree::common::Sleep(1);
  }

  // Allow the hardware audio buffer to completely play out without cutting off
  if (duration_sec > 1.0) {
    unitree::common::Sleep(static_cast<int>(std::ceil(duration_sec)));
  }

  client.PlayStop(stream_id);
  return 0;
}
