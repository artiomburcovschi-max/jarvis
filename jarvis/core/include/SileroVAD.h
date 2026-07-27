#ifndef SILEROVAD_H
#define SILEROVAD_H

#include <string>
#include <vector>
#include <onnxruntime_cxx_api.h>
class SileroVAD {
public:
    explicit SileroVAD(const std::string& model_path);

    float ProcessFrame(const std::vector<int16_t>& frame);

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
