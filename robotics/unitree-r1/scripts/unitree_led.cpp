// Set the R1 head LED colour: unitree_led <R> <G> <B> [net_interface]
// Silent test/diagnostic tool. The assistant itself drives the LED through the audio daemon
// (/tmp/unitree_led.sock) so it doesn't pay the DDS start-up cost on every change.
#include <cstdlib>
#include <iostream>
#include <unitree/robot/g1/audio/g1_audio_client.hpp>

int main(int argc, char const *argv[]) {
  if (argc < 4) {
    std::cerr << "usage: unitree_led R G B [net_interface]   (0-255 each)" << std::endl;
    return 2;
  }
  const char* net_interface = (argc > 4) ? argv[4] : "eth10";
  unitree::robot::ChannelFactory::Instance()->Init(0, net_interface);
  unitree::robot::g1::AudioClient client;
  client.Init();
  client.SetTimeout(3.0f);
  int32_t ret = client.LedControl(static_cast<uint8_t>(atoi(argv[1])), static_cast<uint8_t>(atoi(argv[2])),
                                  static_cast<uint8_t>(atoi(argv[3])));
  std::cout << "LedControl(" << argv[1] << "," << argv[2] << "," << argv[3] << ") ret=" << ret << std::endl;
  return ret == 0 ? 0 : 1;
}
