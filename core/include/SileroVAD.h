#ifndef SILEROVAD_H
#define SILEROVAD_H

#include <string>
#include <vector>
#include <onnxruntime_cxx_api.h>

// Обёртка над Silero VAD (ONNX, v5).
//
// ВАЖНО: модель v5 ожидает на вход не 512 сэмплов, а 576 = 64 (контекст
// от предыдущего кадра) + 512 (новый кадр). Без этого context'а модель
// работает и не падает (входной тензор задан с динамической формой),
// но выдаёт заниженную вероятность речи почти на любом сигнале.
class SileroVAD {
public:
    explicit SileroVAD(const std::string& model_path);

    // Возвращает вероятность речи в кадре (0.0-1.0). frame.size() должен быть 512.
    float ProcessFrame(const std::vector<int16_t>& frame);

    // Сбрасывает скрытое состояние RNN и контекст (например, при старте программы).
    void Reset();

private:
    Ort::Env env;
    Ort::SessionOptions session_options;
    Ort::Session session;

    std::vector<int64_t> input_node_dims;  // {1, 576}
    std::vector<int64_t> state_node_dims;  // {2, 1, 128}
    std::vector<int64_t> sr_node_dims;     // {1}

    std::vector<float> _state;    // скрытое состояние RNN (2*1*128)
    std::vector<float> _context;  // последние 64 сэмпла предыдущего кадра
    int64_t _sr;                  // строго int64_t

    static constexpr size_t kFrameSamples = 512;
    static constexpr size_t kContextSamples = 64;
};

#endif // SILEROVAD_H
