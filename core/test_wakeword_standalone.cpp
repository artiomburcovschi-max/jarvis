#include "WakeWordDetector.h"
#include <iostream>
#include <fstream>
#include <vector>
#include <cstring>
#include <cstdint>

// Минимальный ридер WAV (PCM16 mono) - без внешних зависимостей, только для
// этого разового теста сверки с Python-эталоном.
std::vector<int16_t> ReadWavPCM16(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { std::cerr << "Не открылся файл: " << path << std::endl; std::exit(1); }
    char riff[4]; f.read(riff, 4);
    f.seekg(40, std::ios::beg);  // пропускаем стандартный 44-байтный WAV-заголовок до "data"+size
    // На случай нестандартных чанков ищем "data" честно:
    f.seekg(12, std::ios::beg);
    char chunk_id[4];
    uint32_t chunk_size;
    while (f.read(chunk_id, 4)) {
        f.read(reinterpret_cast<char*>(&chunk_size), 4);
        if (std::strncmp(chunk_id, "data", 4) == 0) {
            std::vector<int16_t> samples(chunk_size / 2);
            f.read(reinterpret_cast<char*>(samples.data()), chunk_size);
            return samples;
        }
        f.seekg(chunk_size, std::ios::cur);
    }
    std::cerr << "Не нашёл data-чанк в " << path << std::endl;
    std::exit(1);
}

void RunOnFile(WakeWordDetector& detector, const std::string& path) {
    detector.Reset();
    auto samples = ReadWavPCM16(path);

    const size_t frame_size = 512;
    float max_score = -1.0f;
    int predictions = 0;
    std::vector<float> all_scores;

    for (size_t i = 0; i + frame_size <= samples.size(); i += frame_size) {
        std::vector<int16_t> frame(samples.begin() + i, samples.begin() + i + frame_size);
        float score = detector.ProcessFrame(frame);
        if (score >= 0.0f) {
            predictions++;
            all_scores.push_back(score);
            if (score > max_score) max_score = score;
        }
    }

    std::cout << path << ": предсказаний=" << predictions << " max=" << max_score << std::endl;
    std::cout << "  скоры: ";
    for (float s : all_scores) std::cout << s << " ";
    std::cout << std::endl;
}

int main(int argc, char* argv[]) {
    if (argc < 5) {
        std::cerr << "Использование: " << argv[0]
                  << " <melspectrogram.onnx> <embedding_model.onnx> <wakeword.onnx> <file1.wav> [file2.wav ...]\n\n"
                  << "Все WAV должны быть 16-bit PCM, mono, 16000 Hz (см. README.md, раздел\n"
                  << "\"Раунд 14: wake-word (B2)\" - там же ссылки, откуда взять "
                  << "melspectrogram.onnx/embedding_model.onnx, и как получить/обучить свою "
                  << "модель слова-активатора).\n";
        return 1;
    }

    WakeWordDetector detector(argv[1], argv[2], argv[3]);
    if (!detector.IsEnabled()) {
        std::cerr << "Детектор не включился - проверьте пути к моделям." << std::endl;
        return 1;
    }

    for (int i = 4; i < argc; ++i) {
        RunOnFile(detector, argv[i]);
    }

    return 0;
}
