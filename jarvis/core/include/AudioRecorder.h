#ifndef AUDIO_RECORDER_H
#define AUDIO_RECORDER_H

#include <vector>
#include <cstdint>
#include <string>
#include <portaudio.h>

class AudioRecorder {
public:
    AudioRecorder();
    ~AudioRecorder();

    bool Start(const std::string& deviceNameHint = "default");
    void Stop();
    bool ReadFrame(std::vector<int16_t>& frame);

    static int FindDeviceIdByName(const std::string& nameSubstr);

private:
    PaStream* stream;
    bool isRunning;
};

#endif // AUDIO_RECORDER_H
