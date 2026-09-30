#include <iostream>
#include <fstream>
#include <vector>
#include <string>
#include <thread>
#include <chrono>
#include <mutex>
#include <atomic>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <unitree/robot/go2/video/video_client.hpp>

// Snapshot is written ~25x/sec, so keep it in RAM (/dev/shm) instead of /tmp on the NVMe root fs
#define SNAP_PATH "/dev/shm/unitree_head_camera.jpg"
#define SNAP_TMP_PATH "/dev/shm/unitree_head_camera.tmp"
#define SOCK_PATH "/tmp/unitree_head_camera.sock"

// Frames older than this are treated as stale (DDS video stream stopped) and not served
static const auto kMaxFrameAge = std::chrono::milliseconds(1000);

using Clock = std::chrono::steady_clock;

std::vector<uint8_t> g_latest_frame;
Clock::time_point g_latest_frame_time;
std::mutex g_frame_mutex;
std::atomic<bool> g_running(true);

// Writes the whole buffer; MSG_NOSIGNAL so a disconnected client can't SIGPIPE the daemon
static bool send_all(int fd, const void* data, size_t len) {
    const uint8_t* p = static_cast<const uint8_t*>(data);
    while (len > 0) {
        ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
        if (n <= 0) return false;
        p += n;
        len -= static_cast<size_t>(n);
    }
    return true;
}

void camera_worker(const std::string& net_if) {
    std::cout << "[HEAD_CAMERA] Initializing VideoClient on " << net_if << "..." << std::endl;
    unitree::robot::ChannelFactory::Instance()->Init(0, net_if);
    unitree::robot::go2::VideoClient video_client;
    video_client.SetTimeout(1.0f);
    video_client.Init();

    std::vector<uint8_t> sample;
    int frame_count = 0;
    int error_count = 0;
    while (g_running) {
        int ret = video_client.GetImageSample(sample);
        if (ret == 0 && sample.size() > 0) {
            {
                std::lock_guard<std::mutex> lock(g_frame_mutex);
                g_latest_frame = sample;
                g_latest_frame_time = Clock::now();
            }
            // Atomically update the RAM snapshot
            std::ofstream f(SNAP_TMP_PATH, std::ios::binary);
            if (f.is_open()) {
                f.write(reinterpret_cast<const char*>(sample.data()), sample.size());
                f.close();
                rename(SNAP_TMP_PATH, SNAP_PATH);
            }
            frame_count++;
            error_count = 0;
            if (frame_count == 1 || frame_count % 100 == 0) {
                std::cout << "[HEAD_CAMERA] Live stream active (Frame #" << frame_count << ", Size: " << sample.size() << " bytes)" << std::endl;
            }
        } else {
            error_count++;
            if (error_count == 1 || error_count % 100 == 0) {
                std::cerr << "[HEAD_CAMERA] GetImageSample failed (ret=" << ret << ", consecutive=" << error_count << ")" << std::endl;
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(40)); // ~25 FPS
    }
}

int main(int argc, char** argv) {
    std::string net_if = "eth10";
    if (argc >= 2) {
        net_if = argv[1];
    }

    signal(SIGPIPE, SIG_IGN);

    std::cout << "============================================================" << std::endl;
    std::cout << "[INIT] Starting Unitree Head Eye Camera Daemon (" << net_if << ")" << std::endl;
    std::cout << "============================================================" << std::endl;

    std::thread th(camera_worker, net_if);
    th.detach();

    // Setup UNIX domain socket for ultra-fast instant zero-copy frame retrieval (<1ms)
    unlink(SOCK_PATH);
    int server_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (server_fd < 0) {
        std::cerr << "[ERROR] Could not create unix socket" << std::endl;
        return 1;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCK_PATH, sizeof(addr.sun_path) - 1);

    if (bind(server_fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        std::cerr << "[ERROR] Could not bind unix socket" << std::endl;
        return 1;
    }

    if (listen(server_fd, 10) < 0) {
        std::cerr << "[ERROR] Could not listen on unix socket" << std::endl;
        return 1;
    }

    std::cout << "[HEAD_CAMERA] ✅ Daemon ready! Socket: " << SOCK_PATH << ", Snap: " << SNAP_PATH << std::endl;

    while (g_running) {
        int client_fd = accept(server_fd, NULL, NULL);
        if (client_fd >= 0) {
            std::vector<uint8_t> frame_copy;
            {
                std::lock_guard<std::mutex> lock(g_frame_mutex);
                // Only serve a frame if it is fresh; size 0 tells the client there is no live frame
                if (!g_latest_frame.empty() && Clock::now() - g_latest_frame_time <= kMaxFrameAge) {
                    frame_copy = g_latest_frame;
                }
            }
            uint32_t size = static_cast<uint32_t>(frame_copy.size());
            if (send_all(client_fd, &size, sizeof(size)) && size > 0) {
                send_all(client_fd, frame_copy.data(), frame_copy.size());
            }
            close(client_fd);
        }
    }

    close(server_fd);
    unlink(SOCK_PATH);
    return 0;
}
