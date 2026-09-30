#include <iostream>
#include <vector>
#include <queue>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <chrono>
#include <atomic>
#include <algorithm>
#include <csignal>
#include <cstring>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <unitree/common/time/time_tool.hpp>
#include <unitree/robot/g1/audio/g1_audio_client.hpp>

#define SOCKET_PATH "/tmp/unitree_audio.sock"
#define STREAM_CHUNK_SIZE 32000 // 1 sec at 16kHz 16-bit mono
#define BYTES_PER_SEC (16000.0 * 2.0)

// Send the next utterance this long before the current one finishes, so the robot-side
// buffer never runs dry between sentences (gapless chaining).
static const auto kSendLead = std::chrono::milliseconds(150);
// After the last audio has finished playing, wait this long for more audio before PlayStop.
// Prevents cutting off the tail of an utterance and restarting the stream between sentences.
// Must exceed the send->speaker latency (measured ~0.32 s on the R1), since playback_end is
// estimated from send time: the old 95%-of-duration PlayStop cut ~0.5 s off every utterance.
static const auto kStopGrace = std::chrono::milliseconds(700);

using Clock = std::chrono::steady_clock;

std::queue<std::vector<uint8_t>> g_audio_queue;
std::mutex g_queue_mutex;
std::condition_variable g_queue_cv;
std::atomic<bool> g_running(true);

void dds_player_thread(const char* net_interface) {
  unitree::robot::ChannelFactory::Instance()->Init(0, net_interface);
  unitree::robot::g1::AudioClient client;
  client.Init();
  client.SetTimeout(5.0f);
  client.SetVolume(100);

  std::string current_stream_id = "";
  bool is_streaming = false;
  // Estimated time at which the speaker finishes everything sent so far on this stream
  Clock::time_point playback_end = Clock::now();

  while (g_running) {
    std::vector<uint8_t> pcm;
    {
      std::unique_lock<std::mutex> lock(g_queue_mutex);
      if (g_audio_queue.empty()) {
        if (is_streaming && Clock::now() >= playback_end + kStopGrace) {
          // Speaker has finished and no more audio arrived: gracefully end stream
          client.PlayStop(current_stream_id);
          is_streaming = false;
        }
        g_queue_cv.wait_for(lock, std::chrono::milliseconds(20));
        continue;
      }
      pcm = std::move(g_audio_queue.front());
      g_audio_queue.pop();
    }

    if (pcm.empty()) continue;

    if (!is_streaming) {
      current_stream_id = std::to_string(unitree::common::GetCurrentTimeMillisecond());
      is_streaming = true;
      playback_end = Clock::now();
    } else {
      // Pace: don't overrun the robot-side buffer; send just before the current audio ends
      auto send_at = playback_end - kSendLead;
      if (Clock::now() < send_at) {
        std::this_thread::sleep_until(send_at);
      }
    }

    size_t total_size = pcm.size();
    size_t offset = 0;
    while (offset < total_size) {
      size_t remaining = total_size - offset;
      size_t chunk_size = std::min(static_cast<size_t>(STREAM_CHUNK_SIZE), remaining);
      std::vector<uint8_t> chunk(pcm.begin() + offset, pcm.begin() + offset + chunk_size);
      client.PlayStream("tts_output", current_stream_id, chunk);
      offset += chunk_size;
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }

    // Exact audio duration bookkeeping (replaces the old fixed 95% sleep, which truncated the tail)
    auto duration = std::chrono::microseconds(
        static_cast<int64_t>(static_cast<double>(total_size) / BYTES_PER_SEC * 1e6));
    playback_end = std::max(playback_end, Clock::now()) + duration;
  }

  if (is_streaming) {
    client.PlayStop(current_stream_id);
  }
}

int main(int argc, char const *argv[]) {
  const char* net_interface = (argc > 1) ? argv[1] : "eth10";

  // A client that disconnects mid-transfer must not kill the daemon
  signal(SIGPIPE, SIG_IGN);

  unlink(SOCKET_PATH);
  int server_fd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (server_fd < 0) return 1;

  struct sockaddr_un addr;
  memset(&addr, 0, sizeof(addr));
  addr.sun_family = AF_UNIX;
  strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path) - 1);

  if (bind(server_fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) return 1;
  if (listen(server_fd, 20) < 0) return 1;

  std::thread player_worker(dds_player_thread, net_interface);
  player_worker.detach();

  std::cout << "[AUDIO DAEMON] ✅ Non-blocking Gapless Unitree Audio Daemon ready on " << SOCKET_PATH << std::endl;

  while (true) {
    int client_fd = accept(server_fd, NULL, NULL);
    if (client_fd < 0) continue;

    std::vector<uint8_t> pcm;
    uint8_t buffer[4096];
    ssize_t bytes_read;
    while ((bytes_read = read(client_fd, buffer, sizeof(buffer))) > 0) {
      pcm.insert(pcm.end(), buffer, buffer + bytes_read);
    }
    close(client_fd);

    if (pcm.size() % 2 != 0) pcm.pop_back();  // keep int16 sample alignment

    if (!pcm.empty()) {
      std::lock_guard<std::mutex> lock(g_queue_mutex);
      g_audio_queue.push(std::move(pcm));
      g_queue_cv.notify_one();
    }
  }

  close(server_fd);
  unlink(SOCKET_PATH);
  return 0;
}
