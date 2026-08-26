#pragma once

#include <string>
#include <vector>
#include <cstdint>
#include <portaudio.h>


class AudioRecorder{
public:
    ~AudioRecorder();

    //Функция старта включения микрофона и начала записи,принимает строку с названием микрофона.По дефолту стоит - "default"
    bool Start(const std::string& DeviceName = "pulse");
    //Функция остановки(деструктор,строка 11-13)
    void Stop();
    //Функция чтения аудио-потока из функции Start()
    bool ReadStream(std::vector<int16_t>& AudioStorage);
    //Функция-помощник для Start(),перебирает все устройства подключенные к системе и находит нужное при условии что DeviceName != "default"
    int SearchingTargetDeviceId(const std::string& DeviceName);
    //Функция вывода всех доступных микрофонов
    void PrintDevices();
private:
    PaStream* stream = nullptr;
    bool isRunning = false;
};