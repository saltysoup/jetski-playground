#include <iostream>
#include <vector>
#include <queue>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <chrono>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <unitree/common/time/time_tool.hpp>
#include <unitree/robot/g1/audio/g1_audio_client.hpp>

#define SOCKET_PATH "/tmp/unitree_audio.sock"
#define STREAM_CHUNK_SIZE 32000 // 1 sec at 16kHz 16-bit mono

std::queue<std::vector<uint8_t>> g_audio_queue;
std::mutex g_queue_mutex;
std::condition_variable g_queue_cv;
bool g_running = true;

void dds_player_thread(const char* net_interface) {
  unitree::robot::ChannelFactory::Instance()->Init(0, net_interface);
  unitree::robot::g1::AudioClient client;
  client.Init();
  client.SetTimeout(5.0f);
  client.SetVolume(100);

  std::string current_stream_id = "";
  bool is_streaming = false;

  while (g_running) {
    std::vector<uint8_t> pcm;
    {
      std::unique_lock<std::mutex> lock(g_queue_mutex);
      if (g_audio_queue.empty()) {
        if (is_streaming) {
          // No more audio arriving, gracefully end stream
          client.PlayStop(current_stream_id);
          is_streaming = false;
        }
        g_queue_cv.wait_for(lock, std::chrono::milliseconds(50));
        continue;
      }
      pcm = std::move(g_audio_queue.front());
      g_audio_queue.pop();
    }

    if (pcm.empty()) continue;

    if (!is_streaming) {
      current_stream_id = std::to_string(unitree::common::GetCurrentTimeMillisecond());
      is_streaming = true;
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
    
    // Exact audio duration pacing to prevent queue overrun
    double duration_sec = static_cast<double>(total_size) / (16000.0 * 2.0);
    int sleep_ms = static_cast<int>(duration_sec * 950.0); // 95% pacing for seamless chunk chaining
    if (sleep_ms > 20) {
      std::this_thread::sleep_for(std::chrono::milliseconds(sleep_ms));
    }
  }

  if (is_streaming) {
    client.PlayStop(current_stream_id);
  }
}

int main(int argc, char const *argv[]) {
  const char* net_interface = (argc > 1) ? argv[1] : "eth10";

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
