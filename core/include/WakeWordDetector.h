#ifndef WAKEWORDDETECTOR_H
#define WAKEWORDDETECTOR_H

#include <string>
#include <vector>
#include <deque>
#include <memory>
#include <onnxruntime_cxx_api.h>

// Обёртка над конвейер openWakeWord (ONNX): melspectrogram -> embedding ->
// классификатор конкретного слова-активатора. Три модели, а не одна (в
// отличие от SileroVAD) - так устроен сам openWakeWord: melspec и embedding
// общие/переиспользуемые (обучены Google, заморожены), а маленький
// классификатор конкретного слова обучается отдельно поверх них.
//
// Вся арифметика буферизации (когда считать mel-спектрограмму, как именно
// скользит окно эмбеддингов, нормализация выхода mel-модели) СВЕРЕНА с
// официальной реализацией (openwakeword/utils.py, класс AudioFeatures,
// метод _streaming_features) и ПРОВЕРЕНА end-to-end на реальных .onnx-моделях
// и синтезированной речи перед переносом сюда - см. README.md, раздел
// "Раунд 14: wake-word (B2)".
//
// ВАЖНО (легко упустить): mel-модель ожидает на вход int16-сэмплы,
// приведённые к float БЕЗ нормализации на 32768 (в отличие от SileroVAD,
// который делит на 32768.0f!). Подавать сюда нужно СЫРОЙ (не усиленный
// автогейном) кадр - как и то, что в итоге уходит в Whisper.
class WakeWordDetector {
public:
    // wakeword_model_path может быть пустым - тогда IsEnabled() вернёт false
    // и ProcessFrame() будет мгновенным no-op (см. main.cpp: детектор
    // полностью опционален, ядро обязано работать и без него, как раньше,
    // пока нет обученной модели под русское произношение).
    WakeWordDetector(const std::string& melspec_model_path,
                      const std::string& embedding_model_path,
                      const std::string& wakeword_model_path);

    bool IsEnabled() const { return enabled_; }

    // Кормить СЫРЫМИ (не усиленными автогейном) int16-кадрами ЛЮБОГО размера
    // (в проекте - те же 512-сэмпловые кадры, что и у SileroVAD, никакой
    // отдельной нарезки не нужно). Возвращает -1.0f, пока не накоплено
    // достаточно истории для первого предсказания (первые ~1.7с после
    // Reset()) - это НЕ означает "слово не сказано", а означает "ещё рано
    // спрашивать". Как только истории достаточно - реальная вероятность
    // (0.0-1.0) активационного слова в последнем окне.
    float ProcessFrame(const std::vector<int16_t>& frame);

    void Reset();

    // Только для отладки/сверки с эталоном - не часть боевого API.
    size_t DebugMelBufferSize() const { return mel_buffer_.size(); }
    size_t DebugFeatureBufferSize() const { return feature_buffer_.size(); }

private:
    bool enabled_ = false;

    Ort::Env env_;
    Ort::SessionOptions session_options_;
    // unique_ptr, а не прямой Ort::Session - у него нет "пустого" валидного
    // состояния (конструктор сразу пытается загрузить файл модели и бросает
    // исключение, если файла нет). Раз детектор должен уметь быть выключен
    // (пока нет обученной под русское произношение модели), сессии создаются
    // ЛЕНИВО, только если все три файла реально на месте - см. .cpp.
    std::unique_ptr<Ort::Session> melspec_session_;
    std::unique_ptr<Ort::Session> embedding_session_;
    std::unique_ptr<Ort::Session> wakeword_session_;

    // --- параметры конвейера (см. официальный openwakeword/utils.py) ---
    static constexpr size_t kTriggerSamples = 1280;   // 80 мс - копим столько сырых сэмплов перед пересчётом mel
    static constexpr size_t kMelContextPad = 480;     // хвост доп. контекста при пересчёте mel (160*3 в референсе)
    static constexpr size_t kMelFeatures = 32;        // мел-бинов на кадр
    static constexpr size_t kEmbWindowFrames = 76;    // окно mel-кадров на один эмбеддинг
    static constexpr size_t kEmbStepFrames = 8;       // шаг скольжения окна эмбеддинга
    static constexpr size_t kEmbFeatures = 96;        // размер эмбеддинга
    static constexpr size_t kWakewordWindow = 16;     // сколько последних эмбеддингов смотрит классификатор
    static constexpr size_t kRawBufferMaxSamples = 160000;  // 10с - как в референсе
    static constexpr size_t kMelBufferMaxFrames = 970;      // 10с мел-истории
    static constexpr size_t kFeatureBufferMaxFrames = 120;  // ~10с эмбеддингов

    std::deque<int16_t> raw_buffer_;
    std::vector<std::vector<float>> mel_buffer_;      // [T][32], растёт, режется сверху по MaxFrames
    std::vector<std::vector<float>> feature_buffer_;  // [T][96]
    size_t accumulated_samples_ = 0;

    // Считает mel-спектрограмму для ПОСЛЕДНИХ n_samples (+ контекст) сэмплов
    // из raw_buffer_ и ДОКЛЕИВАЕТ результат к mel_buffer_ (не заменяет).
    void ComputeAndAppendMelspectrogram(size_t n_samples);

    // embedding_model на одном окне [76][32] -> вектор из 96 чисел.
    std::vector<float> ComputeEmbedding(const std::vector<std::vector<float>>& window_76x32);

    // classifier на последних kWakewordWindow эмбеддингах -> скор 0..1.
    float ComputeWakewordScore();
};

#endif // WAKEWORDDETECTOR_H
