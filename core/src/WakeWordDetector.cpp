#include "WakeWordDetector.h"
#include <iostream>
#include <fstream>
#include <algorithm>
#include <numeric>

namespace {
    bool FileExists(const std::string& path) {
        std::ifstream f(path);
        return f.good();
    }
}

WakeWordDetector::WakeWordDetector(const std::string& melspec_model_path,
                                    const std::string& embedding_model_path,
                                    const std::string& wakeword_model_path)
    : env_(ORT_LOGGING_LEVEL_WARNING, "WakeWordDetector"),
      session_options_() {

    if (wakeword_model_path.empty()) {
        std::cout << "[WakeWord] Модель слова-активатора не задана - детектор ВЫКЛЮЧЕН "
                     "(ядро продолжит работать как раньше, через Whisper+fuzzy-поиск)."
                  << std::endl;
        return;
    }

    if (!FileExists(melspec_model_path) || !FileExists(embedding_model_path) ||
        !FileExists(wakeword_model_path)) {
        std::cout << "[WakeWord] Не найден один из ONNX-файлов конвейера "
                     "(melspectrogram/embedding/wakeword) - детектор ВЫКЛЮЧЕН." << std::endl;
        return;
    }

    session_options_.SetIntraOpNumThreads(1);
    session_options_.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);

    try {
        melspec_session_ = std::make_unique<Ort::Session>(env_, melspec_model_path.c_str(), session_options_);
        embedding_session_ = std::make_unique<Ort::Session>(env_, embedding_model_path.c_str(), session_options_);
        wakeword_session_ = std::make_unique<Ort::Session>(env_, wakeword_model_path.c_str(), session_options_);
    } catch (const std::exception& e) {
        std::cerr << "[WakeWord] Ошибка загрузки ONNX-моделей: " << e.what()
                  << " - детектор ВЫКЛЮЧЕН, ядро продолжит работать без него." << std::endl;
        melspec_session_.reset();
        embedding_session_.reset();
        wakeword_session_.reset();
        return;
    }

    enabled_ = true;
    Reset();
    std::cout << "[WakeWord] Детектор слова-активатора включён." << std::endl;
}

void WakeWordDetector::Reset() {
    raw_buffer_.clear();
    // Стартовая затравка mel-буфера (76 "нейтральных" кадров) - как в
    // референсной реализации (openwakeword/utils.py: melspectrogram_buffer
    // = np.ones((76, 32))) - без неё первое окно эмбеддинга не набралось бы
    // ДО прихода первых 76 реальных кадров, а так классификатор может
    // получить свои первые (неинформативные, но валидные по форме) окна
    // почти сразу после старта, вместо "тишины" на входе.
    mel_buffer_.assign(kEmbWindowFrames, std::vector<float>(kMelFeatures, 1.0f));
    feature_buffer_.clear();
    accumulated_samples_ = 0;
}

float WakeWordDetector::ProcessFrame(const std::vector<int16_t>& frame) {
    if (!enabled_ || frame.empty()) return -1.0f;

    // 1. Копим сырые сэмплы (нужен хвост контекста для mel-модели - см. ниже).
    raw_buffer_.insert(raw_buffer_.end(), frame.begin(), frame.end());
    if (raw_buffer_.size() > kRawBufferMaxSamples) {
        raw_buffer_.erase(raw_buffer_.begin(), raw_buffer_.begin() + (raw_buffer_.size() - kRawBufferMaxSamples));
    }
    accumulated_samples_ += frame.size();

    // 2. Пересчитываем mel-спектрограмму только раз в ~80мс (kTriggerSamples) -
    //    как и в референсе, а не на каждый маленький кадр - существенно
    //    дешевле, и mel-модель всё равно рассчитана на чуть больший кусок.
    if (accumulated_samples_ >= kTriggerSamples) {
        ComputeAndAppendMelspectrogram(accumulated_samples_);

        // 3. Новых 80-мс шагов могло накопиться несколько (если предыдущий
        //    вызов почему-то задержался) - разворачиваем окно эмбеддинга
        //    ОТ САМОГО СТАРОГО из новых шагов К САМОМУ СВЕЖЕМУ, шагом
        //    kEmbStepFrames (8 mel-кадров = ровно kTriggerSamples сэмплов),
        //    как в официальной реализации (ndx = -8*i, i считает ВНИЗ).
        size_t steps = accumulated_samples_ / kTriggerSamples;
        for (size_t i = steps; i-- > 0; ) {
            long ndx = -static_cast<long>(kEmbStepFrames) * static_cast<long>(i);
            size_t end_index = (ndx == 0) ? mel_buffer_.size()
                                           : mel_buffer_.size() + static_cast<size_t>(ndx);
            if (end_index < kEmbWindowFrames) continue;  // ещё недостаточно истории
            size_t start_index = end_index - kEmbWindowFrames;

            std::vector<std::vector<float>> window(mel_buffer_.begin() + static_cast<long>(start_index),
                                                    mel_buffer_.begin() + static_cast<long>(end_index));
            feature_buffer_.push_back(ComputeEmbedding(window));
        }

        accumulated_samples_ = 0;
    }

    if (feature_buffer_.size() > kFeatureBufferMaxFrames) {
        feature_buffer_.erase(feature_buffer_.begin(),
                               feature_buffer_.begin() + (feature_buffer_.size() - kFeatureBufferMaxFrames));
    }

    if (feature_buffer_.size() < kWakewordWindow) {
        return -1.0f;  // ещё не набралось истории для первого предсказания
    }

    return ComputeWakewordScore();
}

void WakeWordDetector::ComputeAndAppendMelspectrogram(size_t n_samples) {
    // Берём хвост raw_buffer_: n_samples новых + kMelContextPad сэмплов
    // контекста ДО них (как в референсе: list(raw_data_buffer)[-n_samples-480:]).
    size_t take = std::min(raw_buffer_.size(), n_samples + kMelContextPad);
    std::vector<float> input_floats(take);
    auto start_it = raw_buffer_.end() - static_cast<long>(take);
    // ВАЖНО: приведение int16 -> float БЕЗ деления на 32768 - подтверждено
    // по исходникам openwakeword/utils.py (_get_melspectrogram): модель
    // ожидает "сырую" амплитуду, а не нормализованную в [-1, 1].
    std::transform(start_it, raw_buffer_.end(), input_floats.begin(),
                    [](int16_t s) { return static_cast<float>(s); });

    Ort::MemoryInfo memory_info = Ort::MemoryInfo::CreateCpu(OrtDeviceAllocator, OrtMemTypeDefault);
    std::vector<int64_t> input_dims = {1, static_cast<int64_t>(input_floats.size())};
    Ort::Value input_tensor = Ort::Value::CreateTensor<float>(
        memory_info, input_floats.data(), input_floats.size(), input_dims.data(), input_dims.size());

    const char* input_names[] = {"input"};
    const char* output_names[] = {"output"};

    auto output_tensors = melspec_session_->Run(Ort::RunOptions{nullptr}, input_names, &input_tensor, 1,
                                                 output_names, 1);

    // Реальная (числовая, не символьная!) форма выхода - (1, 1, time, 32),
    // ПРОВЕРЕНО инспекцией на живой модели с разными длинами входа - имена
    // символьных осей в метаданных ONNX ('time' первой) вводят в
    // заблуждение: по факту time - ТРЕТЬЯ ось, а не первая (первые две -
    // всегда 1). Так как обе размерности 1 не сдвигают плоский offset,
    // индексация data[t*32+m] ниже остаётся верной - меняется только то,
    // ОТКУДА берётся сама длина time_frames.
    auto shape = output_tensors[0].GetTensorTypeAndShapeInfo().GetShape();
    const float* data = output_tensors[0].GetTensorData<float>();
    int64_t time_frames = shape[2];

    for (int64_t t = 0; t < time_frames; ++t) {
        std::vector<float> mel_frame(kMelFeatures);
        for (size_t m = 0; m < kMelFeatures; ++m) {
            // Обязательная ручная трансформация выхода mel-модели - НЕ часть
            // ONNX-графа, подтверждена по официальному коду (melspec_transform
            // = lambda x: x/10 + 2). Без неё embedding-модель получает
            // сигнал в чужом масштабе и выдаёт бессмысленные эмбеддинги.
            mel_frame[m] = data[t * static_cast<int64_t>(kMelFeatures) + static_cast<int64_t>(m)] / 10.0f + 2.0f;
        }
        mel_buffer_.push_back(std::move(mel_frame));
    }

    if (mel_buffer_.size() > kMelBufferMaxFrames) {
        mel_buffer_.erase(mel_buffer_.begin(), mel_buffer_.begin() + (mel_buffer_.size() - kMelBufferMaxFrames));
    }
}

std::vector<float> WakeWordDetector::ComputeEmbedding(const std::vector<std::vector<float>>& window_76x32) {
    std::vector<float> flat(kEmbWindowFrames * kMelFeatures);
    for (size_t t = 0; t < kEmbWindowFrames; ++t) {
        std::copy(window_76x32[t].begin(), window_76x32[t].end(), flat.begin() + static_cast<long>(t * kMelFeatures));
    }

    Ort::MemoryInfo memory_info = Ort::MemoryInfo::CreateCpu(OrtDeviceAllocator, OrtMemTypeDefault);
    // Форма (1, 76, 32, 1) - подтверждена инспекцией реальной модели.
    std::vector<int64_t> input_dims = {1, static_cast<int64_t>(kEmbWindowFrames),
                                        static_cast<int64_t>(kMelFeatures), 1};
    Ort::Value input_tensor = Ort::Value::CreateTensor<float>(
        memory_info, flat.data(), flat.size(), input_dims.data(), input_dims.size());

    const char* input_names[] = {"input_1"};
    const char* output_names[] = {"conv2d_19"};

    auto output_tensors = embedding_session_->Run(Ort::RunOptions{nullptr}, input_names, &input_tensor, 1,
                                                   output_names, 1);

    const float* data = output_tensors[0].GetTensorData<float>();
    return std::vector<float>(data, data + kEmbFeatures);
}

float WakeWordDetector::ComputeWakewordScore() {
    std::vector<float> flat(kWakewordWindow * kEmbFeatures);
    size_t start = feature_buffer_.size() - kWakewordWindow;
    for (size_t t = 0; t < kWakewordWindow; ++t) {
        std::copy(feature_buffer_[start + t].begin(), feature_buffer_[start + t].end(),
                   flat.begin() + static_cast<long>(t * kEmbFeatures));
    }

    Ort::MemoryInfo memory_info = Ort::MemoryInfo::CreateCpu(OrtDeviceAllocator, OrtMemTypeDefault);
    std::vector<int64_t> input_dims = {1, static_cast<int64_t>(kWakewordWindow), static_cast<int64_t>(kEmbFeatures)};
    Ort::Value input_tensor = Ort::Value::CreateTensor<float>(
        memory_info, flat.data(), flat.size(), input_dims.data(), input_dims.size());

    // ВНИМАНИЕ: имена входа/выхода классификатора ЗАВИСЯТ от конкретной
    // обученной модели (у "hey_jarvis_v0.1.onnx" это "x.1"/"53" - служебные
    // авто-сгенерированные ONNX-имена, не осмысленные). Имя входа не менялось
    // ни разу в моделях сообщества openWakeWord (все экспортированы одним и
    // тем же пайплайном), но при подключении СВОЕЙ обученной модели стоит
    // сверить через Netron/onnxruntime, если детектор не заработает.
    const char* input_names[] = {"x.1"};
    const char* output_names[] = {"53"};

    auto output_tensors = wakeword_session_->Run(Ort::RunOptions{nullptr}, input_names, &input_tensor, 1,
                                                  output_names, 1);
    return output_tensors[0].GetTensorData<float>()[0];
}
