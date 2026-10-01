// R1 hand bridge: lets the Python assistant drive the BrainCo Revo2 hands without a Python DDS stack.
//
//   Python  --"left q0 q1 q2 q3 q4 q5 [speed]"-->  /tmp/unitree_hand.sock  -->  rt/brainco/left/cmd
//   Python  --"state"-->  reply "left <age_ms> q0..q5 | right <age_ms> q0..q5"  (age -1 = never seen)
//
// rt/brainco/{left,right}/{cmd,state} are served by Unitree's brainco_hand_server (serial <-> DDS).
// Finger order: thumb, thumb_aux, index, middle, ring, pinky. Position 0 = open, 1 = closed; speed 0..1.
// Usage: unitree_hand_bridge [net_interface]   (default eth10)
#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#include <unitree/idl/go2/MotorCmds_.hpp>
#include <unitree/idl/go2/MotorStates_.hpp>
#include <unitree/robot/channel/channel_publisher.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>

#define HAND_SOCKET_PATH "/tmp/unitree_hand.sock"

using Clock = std::chrono::steady_clock;
using unitree_go::msg::dds_::MotorCmds_;
using unitree_go::msg::dds_::MotorStates_;

struct HandState {
  std::mutex mutex;
  float q[6] = {0, 0, 0, 0, 0, 0};
  Clock::time_point seen;
  bool ever = false;
};

static HandState g_state[2];  // 0 = left, 1 = right
static std::atomic<bool> g_running(true);

static void on_state(int side, const void* msg) {
  const auto* s = static_cast<const MotorStates_*>(msg);
  std::lock_guard<std::mutex> lock(g_state[side].mutex);
  for (size_t i = 0; i < 6 && i < s->states().size(); ++i) g_state[side].q[i] = s->states()[i].q();
  g_state[side].seen = Clock::now();
  g_state[side].ever = true;
}

static std::string describe(int side) {
  std::lock_guard<std::mutex> lock(g_state[side].mutex);
  long age = g_state[side].ever
                 ? (long)std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - g_state[side].seen).count()
                 : -1;
  char buf[160];
  snprintf(buf, sizeof(buf), "%s %ld %.3f %.3f %.3f %.3f %.3f %.3f", side ? "right" : "left", age, g_state[side].q[0],
           g_state[side].q[1], g_state[side].q[2], g_state[side].q[3], g_state[side].q[4], g_state[side].q[5]);
  return buf;
}

int main(int argc, char const* argv[]) {
  const char* net_interface = (argc > 1) ? argv[1] : "eth10";
  signal(SIGINT, [](int) { g_running = false; });
  signal(SIGTERM, [](int) { g_running = false; });

  unitree::robot::ChannelFactory::Instance()->Init(0, net_interface);
  unitree::robot::ChannelPublisher<MotorCmds_> pub_left("rt/brainco/left/cmd");
  unitree::robot::ChannelPublisher<MotorCmds_> pub_right("rt/brainco/right/cmd");
  pub_left.InitChannel();
  pub_right.InitChannel();
  unitree::robot::ChannelSubscriber<MotorStates_> sub_left("rt/brainco/left/state");
  unitree::robot::ChannelSubscriber<MotorStates_> sub_right("rt/brainco/right/state");
  sub_left.InitChannel([](const void* m) { on_state(0, m); }, 1);
  sub_right.InitChannel([](const void* m) { on_state(1, m); }, 1);

  unlink(HAND_SOCKET_PATH);
  int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
  if (fd < 0) return 1;
  struct sockaddr_un addr;
  memset(&addr, 0, sizeof(addr));
  addr.sun_family = AF_UNIX;
  strncpy(addr.sun_path, HAND_SOCKET_PATH, sizeof(addr.sun_path) - 1);
  if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
    std::cerr << "[HAND BRIDGE] bind failed on " << HAND_SOCKET_PATH << std::endl;
    return 1;
  }
  chmod(HAND_SOCKET_PATH, 0666);
  struct timeval tv = {0, 200000};  // wake up regularly to notice SIGTERM
  setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
  std::cout << "[HAND BRIDGE] ✅ ready on " << HAND_SOCKET_PATH << " (DDS on " << net_interface << ")" << std::endl;

  MotorCmds_ cmd;
  cmd.cmds().resize(6);
  char buf[256];
  while (g_running) {
    struct sockaddr_un from;
    socklen_t from_len = sizeof(from);
    ssize_t n = recvfrom(fd, buf, sizeof(buf) - 1, 0, (struct sockaddr*)&from, &from_len);
    if (n <= 0) continue;
    buf[n] = 0;
    std::istringstream in(buf);
    std::string side;
    in >> side;
    if (side == "state") {
      std::string reply = describe(0) + " | " + describe(1);
      if (from_len > sizeof(sa_family_t)) {
        sendto(fd, reply.data(), reply.size(), 0, (struct sockaddr*)&from, from_len);
      }
      continue;
    }
    if (side != "left" && side != "right") continue;
    float q[6];
    float speed = 1.0f;
    bool ok = true;
    for (int i = 0; i < 6; ++i) ok = ok && static_cast<bool>(in >> q[i]);
    if (!ok) continue;
    if (!(in >> speed)) speed = 1.0f;
    for (int i = 0; i < 6; ++i) {
      cmd.cmds()[i].q() = std::max(0.0f, std::min(1.0f, q[i]));
      cmd.cmds()[i].dq() = std::max(0.05f, std::min(1.0f, speed));
    }
    (side == "left" ? pub_left : pub_right).Write(cmd);
  }
  close(fd);
  unlink(HAND_SOCKET_PATH);
  return 0;
}
