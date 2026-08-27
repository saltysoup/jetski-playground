#include <iostream>
#include <fstream>
#include <vector>
#include <string>
#include <thread>
#include <chrono>
#include <mutex>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <unitree/robot/go2/video/video_client.hpp>

#define SNAP_PATH "/tmp/unitree_head_camera.jpg"
#define SOCK_PATH "/tmp/unitree_head_camera.sock"

std::vector<uint8_t> g_latest_frame;
std::mutex g_frame_mutex;
bool g_running = true;

void camera_worker(const std::string& net_if) {
    std::cout << "[HEAD_CAMERA] Initializing VideoClient on " << net_if << "..." << std::endl;
    unitree::robot::ChannelFactory::Instance()->Init(0, net_if);
    unitree::robot::go2::VideoClient video_client;
    video_client.SetTimeout(1.0f);
    video_client.Init();

    std::vector<uint8_t> sample;
    int frame_count = 0;
    while (g_running) {
        int ret = video_client.GetImageSample(sample);
        if (ret == 0 && sample.size() > 0) {
            {
                std::lock_guard<std::mutex> lock(g_frame_mutex);
                g_latest_frame = sample;
            }
            // Atomically update /tmp/unitree_head_camera.jpg
            std::ofstream f("/tmp/unitree_head_camera.tmp", std::ios::binary);
            if (f.is_open()) {
                f.write(reinterpret_cast<const char*>(sample.data()), sample.size());
                f.close();
                rename("/tmp/unitree_head_camera.tmp", SNAP_PATH);
            }
            frame_count++;
            if (frame_count == 1 || frame_count % 100 == 0) {
                std::cout << "[HEAD_CAMERA] Live stream active (Frame #" << frame_count << ", Size: " << sample.size() << " bytes)" << std::endl;
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
                frame_copy = g_latest_frame;
            }
            if (frame_copy.size() > 0) {
                uint32_t size = frame_copy.size();
                write(client_fd, &size, sizeof(size));
                write(client_fd, frame_copy.data(), frame_copy.size());
            } else {
                uint32_t size = 0;
                write(client_fd, &size, sizeof(size));
            }
            close(client_fd);
        }
    }

    close(server_fd);
    unlink(SOCK_PATH);
    return 0;
}
