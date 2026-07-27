#include "SileroVAD.h"
#include <iostream>
#include <algorithm>
#include <stdexcept>

SileroVAD::SileroVAD(const std::string& model_path)
    : env(ORT_LOGGING_LEVEL_WARNING, "SileroVAD"),
      session_options(),
      session(env, model_path.c_str(), session_options),
      _sr(16000) {

    session_options.SetIntraOpNumThreads(1);
    session_options.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);

    input_node_dims = {1, static_cast<int64_t>(kContextSamples + kFrameSamples)};
    state_node_dims = {2, 1, 128};
    sr_node_dims = {1};

    Reset();
}

void SileroVAD::Reset() {
    _state.assign(2 * 1 * 128, 0.0f);
    _context.assign(kContextSamples, 0.0f);
}

float SileroVAD::ProcessFrame(const std::vector<int16_t>& frame) {
    if (frame.size() != kFrameSamples) return 0.0f;

    // Склеиваем: [контекст 64] + [новый кадр 512] = 576
    std::vector<float> input_frame(kContextSamples + kFrameSamples);
    std::copy(_context.begin(), _context.end(), input_frame.begin());
    for (size_t i = 0; i < kFrameSamples; ++i) {
        input_frame[kContextSamples + i] = static_cast<float>(frame[i]) / 32768.0f;
    }

    Ort::MemoryInfo memory_info = Ort::MemoryInfo::CreateCpu(OrtDeviceAllocator, OrtMemTypeDefault);

    int64_t sr_value = _sr;

    std::vector<Ort::Value> input_tensors;
    input_tensors.push_back(Ort::Value::CreateTensor<float>(
        memory_info, input_frame.data(), input_frame.size(), input_node_dims.data(), input_node_dims.size()));

    input_tensors.push_back(Ort::Value::CreateTensor<float>(
        memory_info, _state.data(), _state.size(), state_node_dims.data(), state_node_dims.size()));

    input_tensors.push_back(Ort::Value::CreateTensor<int64_t>(
        memory_info, &sr_value, 1, sr_node_dims.data(), sr_node_dims.size()));

    const char* input_names[] = {"input", "state", "sr"};
    const char* output_names[] = {"output", "stateN"};

    try {
        auto output_tensors = session.Run(Ort::RunOptions{nullptr}, input_names, input_tensors.data(), 3, output_names, 2);

        float speech_prob = output_tensors[0].GetTensorData<float>()[0];

        const float* state_data = output_tensors[1].GetTensorData<float>();
        std::copy(state_data, state_data + _state.size(), _state.begin());

        std::copy(input_frame.end() - static_cast<long>(kContextSamples), input_frame.end(), _context.begin());

        return speech_prob;
    } catch (const std::exception& e) {
        std::cerr << "[SileroVAD] Ошибка ONNX Runtime: " << e.what() << std::endl;
        return 0.0f;
    }
}
