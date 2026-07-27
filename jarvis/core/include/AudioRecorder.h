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

    // Запуск записи. Можно передать подстроку имени ("pulse", "picun", "default")
    bool Start(const std::string& deviceNameHint = "default");
    void Stop();
    bool ReadFrame(std::vector<int16_t>& frame);

    // Вспомогательный метод для поиска ID по имени.
    // ВАЖНО: предполагает, что Pa_Initialize() уже вызван (см. Start()) -
    // сам не инициализирует и не терминирует PortAudio, чтобы не плодить
    // несбалансированные Pa_Initialize()/Pa_Terminate() при внутреннем вызове.
    static int FindDeviceIdByName(const std::string& nameSubstr);

private:
    PaStream* stream;
    bool isRunning;
};

#endif // AUDIO_RECORDER_H