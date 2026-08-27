#include <fstream>
#include <iostream>
#include <thread>
#include <algorithm>
#include <vector>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <netdb.h>
#include <unistd.h>

#include <unitree/common/time/time_tool.hpp>
#include <unitree/idl/ros2/String_.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/robot/g1/audio/g1_audio_client.hpp>
#include "wav.hpp"

#define RECORDING_FILE "record.wav"
#define GROUP_IP "239.168.123.161"
#define PORT 5555
#define WAV_SECOND 5  // record 5 seconds
#define WAV_LEN (16000 * 2 * WAV_SECOND)
#define WAV_LEN_ONCE (16000 * 2 * 160 / 1000)
#define CHUNK_SIZE 96000  // 3 seconds of stream

std::string get_local_ip_for_multicast() {
  struct ifaddrs *ifaddr, *ifa;
  char host[NI_MAXHOST];
  std::string result = "";
  getifaddrs(&ifaddr);
  for (ifa = ifaddr; ifa != nullptr; ifa = ifa->ifa_next) {
    if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET) continue;
    getnameinfo(ifa->ifa_addr, sizeof(struct sockaddr_in), host, NI_MAXHOST,
                NULL, 0, NI_NUMERICHOST);
    std::string ip(host);
    if (ip.find("192.168.123.") == 0) {
      result = ip;
      break;
    }
  }
  freeifaddrs(ifaddr);
  return result;
}

int main(int argc, char const *argv[]) {
  if (argc < 2) {
    std::cout << "Usage: injae_audio_example [NetWorkInterface(eth10)]" << std::endl;
    exit(0);
  }

  int32_t ret;

  /* Initialize ChannelFactory */
  unitree::robot::ChannelFactory::Instance()->Init(0, argv[1]);

  unitree::robot::g1::AudioClient client;
  client.Init();
  client.SetTimeout(10.0f);

  /* Set volume to 100% */
  client.SetVolume(100);

  /* 1. Play Custom TTS Greeting */
  std::cout << "[Step 1] Synthesizing TTS Greeting..." << std::endl;
  ret = client.TtsMaker("Hello my name is Jason. Please start recording.", 1); // 1 = English
  std::cout << "TtsMaker API ret: " << ret << std::endl;

  // Wait 4 seconds to let the TTS finish speaking before recording begins
  unitree::common::Sleep(4);

  /* 2. Start Recording (Runs Synchronously) */
  std::cout << "\n[Step 2] Setting up recording socket..." << std::endl;
  int sock = socket(AF_INET, SOCK_DGRAM, 0);
  sockaddr_in local_addr{};
  local_addr.sin_family = AF_INET;
  local_addr.sin_port = htons(PORT);
  local_addr.sin_addr.s_addr = INADDR_ANY;
  bind(sock, (sockaddr *)&local_addr, sizeof(local_addr));

  ip_mreq mreq{};
  inet_pton(AF_INET, GROUP_IP, &mreq.imr_multiaddr);
  std::string local_ip = get_local_ip_for_multicast();
  std::cout << "Local interface IP: " << local_ip << std::endl;
  mreq.imr_interface.s_addr = inet_addr(local_ip.c_str());
  setsockopt(sock, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq));

  int total_bytes = 0;
  std::vector<int16_t> pcm_data;
  pcm_data.reserve(WAV_LEN / 2);

  std::cout << ">>> START RECORDING (Speak clearly into the microphone for 5s) <<<" << std::endl;
  while (total_bytes < WAV_LEN) {
    char buffer[WAV_LEN_ONCE];
    ssize_t len = recvfrom(sock, buffer, sizeof(buffer), 0, nullptr, nullptr);
    if (len > 0) {
      size_t sample_count = len / 2;
      const int16_t *samples = reinterpret_cast<const int16_t *>(buffer);
      pcm_data.insert(pcm_data.end(), samples, samples + sample_count);
      total_bytes += len;
    }
  }
  close(sock); // Always close your sockets when done!

  WriteWave(RECORDING_FILE, 16000, pcm_data.data(), pcm_data.size(), 1);
  std::cout << ">>> RECORDING FINISHED! Saved to " << RECORDING_FILE << " <<<\n" << std::endl;

  unitree::common::Sleep(1);

  /* 3. Play Back the Recorded File */
  std::cout << "[Step 3] Loading recording for playback..." << std::endl;
  int32_t sample_rate = -1;
  int8_t num_channels = 0;
  bool filestate = false;
  std::vector<uint8_t> pcm = ReadWave(RECORDING_FILE, &sample_rate, &num_channels, &filestate);

  std::cout << "WAV File loaded: sample_rate = " << sample_rate 
            << ", channels = " << std::to_string(num_channels) 
            << ", status = " << filestate << ", size = " << pcm.size() << " bytes" << std::endl;

  if (filestate && sample_rate == 16000 && num_channels == 1) {
    std::cout << ">>> PLAYING BACK RECORDED SOUND... <<<" << std::endl;
    size_t total_size = pcm.size();
    size_t offset = 0;
    std::string stream_id = std::to_string(unitree::common::GetCurrentTimeMillisecond());

    while (offset < total_size) {
      size_t remaining = total_size - offset;
      size_t current_chunk_size = std::min(static_cast<size_t>(CHUNK_SIZE), remaining);
      std::vector<uint8_t> chunk(pcm.begin() + offset, pcm.begin() + offset + current_chunk_size);

      client.PlayStream("example", stream_id, chunk);
      unitree::common::Sleep(1);
      offset += current_chunk_size;
    }
    /* ADD THIS LINE HERE: Let the final 3-second buffer finish playing physically! */
    unitree::common::Sleep(4);
    client.PlayStop(stream_id);
    std::cout << ">>> PLAYBACK COMPLETED SUCCESSFULLY! <<<" << std::endl;
  } else {
    std::cout << "[ERROR] Wave file invalid or empty!" << std::endl;
  }

  return 0;
}

