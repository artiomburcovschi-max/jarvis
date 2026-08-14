#include "AudioRecorder.h"
#include <iostream>

AudioRecorder::~AudioRecorder() {
    Stop();
}

//Функция по поиску микрофона для дальнейшего использования в Start(). Возвращает ID устройства или -1, если не найдено.
int AudioRecorder::FindDeviceIdByName(const std::string& nameSubstr) {
    int numDevices = Pa_GetDeviceCount();//  КОЛ-ВО УСТРОЙСТВ
    
    for (int i = 0; i < numDevices; i++) {
        const PaDeviceInfo* info = Pa_GetDeviceInfo(i);
        if (info && info->maxInputChannels > 0) {// Отсекаем колонки и ищем микрофон
            std::string devName = info->name;
            std::cout << "УСТРОЙТВО!!!!!!!!!!!!!!!!!!! :" << devName << std::endl;
            if (devName.find(nameSubstr) != std::string::npos) {
                return i;
            }
        }
    }
    return -1; 
}

bool AudioRecorder::Start(const std::string& deviceNameHint) {
    PaError err = Pa_Initialize();

    if (err != paNoError) {
        std::cerr << "[AudioRecorder] Ошибка PortAudio: " << Pa_GetErrorText(err) << std::endl;
        return false;
    }
    
    int targetDeviceId = -1;

    if (!deviceNameHint.empty() && deviceNameHint != "default") {
        targetDeviceId = FindDeviceIdByName(deviceNameHint);
        if (targetDeviceId != -1) {
            std::cout << "[AudioRecorder] Найдено устройство по запросу '" 
                      << deviceNameHint << "' (ID: " << targetDeviceId << ")" << std::endl;
        } else {
            std::cout << "[AudioRecorder] Устройство '" << deviceNameHint 
                      << "' не найдено, используем дефолтное." << std::endl;
        }
    }
    if (targetDeviceId == -1) {
        targetDeviceId = Pa_GetDefaultInputDevice();
    }

    if (targetDeviceId == paNoDevice) {
        std::cerr << "[AudioRecorder] Ошибка: Нет доступных микрофонов!" << std::endl;
        return false;
    } 

    //Вывод названия микрофона и его айди
    std::cout<<"====================================================="<<std::endl;
    std::cout << "[AudioRecorder] Используется устройство: " 
              << Pa_GetDeviceInfo(targetDeviceId)->name <<"(ID: " << targetDeviceId << ")" << std::endl;
    std::cout<<"====================================================="<<std::endl;

    PaStreamParameters inputParameters;
    inputParameters.device = targetDeviceId;
    inputParameters.channelCount = 1;       // Моно
    inputParameters.sampleFormat = paInt16; // 16-bit PCM
    inputParameters.suggestedLatency = Pa_GetDeviceInfo(targetDeviceId)->defaultLowInputLatency;
    inputParameters.hostApiSpecificStreamInfo = nullptr;

    err = Pa_OpenStream(
        &stream,
        &inputParameters,
        nullptr,
        16000, // 16 kHz
        512,   // 32 ms кадр
        paClipOff,
        nullptr,
        nullptr
    );

    if (err != paNoError) {
        std::cerr << "[AudioRecorder] Ошибка открытия потока: " << Pa_GetErrorText(err) << std::endl;
        return false;
    }

    err = Pa_StartStream(stream);
    if (err != paNoError) {
        std::cerr << "[AudioRecorder] Ошибка запуска потока: " << Pa_GetErrorText(err) << std::endl;
        return false;
    }

    isRunning = true;
    return true;
}

void AudioRecorder::Stop() {
    if (stream) {
        Pa_StopStream(stream);
        Pa_CloseStream(stream);
        stream = nullptr;
    }
    Pa_Terminate();
    isRunning = false;
}

bool AudioRecorder::ReadFrame(std::vector<int16_t>& frame) {
    if (!isRunning || !stream) return false;

    frame.resize(512);
    PaError err = Pa_ReadStream(stream, frame.data(), 512);
    
    if (err != paNoError && err != paInputOverflowed) {
        std::cerr << "[AudioRecorder] Ошибка чтения: " << Pa_GetErrorText(err) << std::endl;
        return false;
    }

    return true;
}