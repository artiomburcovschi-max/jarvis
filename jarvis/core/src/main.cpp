#include <iostream>
#include <vector>
#include <deque>
#include <cmath>
#include <algorithm>
#include <chrono>
#include <thread>
#include <iomanip>
#include <cstring>
#include <csignal>
#include <atomic>
#include <memory>
#include <zmq.hpp>
#include "AudioRecorder.h"
#include "SileroVAD.h"
#include "PhraseSender.h"

namespace {
    std::atomic<bool> g_running{true};

    void HandleSigint(int) {
        g_running = false;
    }
}

int main() {
    std::signal(SIGINT, HandleSigint);
    std::signal(SIGTERM, HandleSigint);

    std::cout << "[Jarvis Core] Инициализация..." << std::endl;

    // 1. ZeroMQ: аудио уходит в Python отдельным потоком через PhraseSender,
    //    чтобы цикл чтения микрофона никогда не блокировался на сети.
    zmq::context_t zmq_context(1);
    std::unique_ptr<PhraseSender> sender;

    try {
        std::cout << "[ZMQ] Подключение к Python-серверу (порт 5555)..." << std::endl;
        sender = std::make_unique<PhraseSender>(zmq_context, "tcp://127.0.0.1:5555");
        std::cout << "[ZMQ] Успешно подключено!" << std::endl;
    } catch (const zmq::error_t& e) {
        std::cerr << "[ОШИБКА ZMQ] Не удалось подключиться: " << e.what() << std::endl;
        return 1;
    }

    SileroVAD vad("../silero_vad.onnx");
    AudioRecorder recorder;

    if (!recorder.Start()) {
        std::cerr << "[ОШИБКА] Не удалось запустить запись с микрофона!" << std::endl;
        return 1;
    }

    std::cout << "\n[Jarvis Core] Готов! Говори в микрофон... (Ctrl+C для выхода)\n" << std::endl;

    // --- Настройки конечного автомата VAD ---
    const float VAD_SPEECH_THRESHOLD = 0.55f;   // порог активации (речь)
    const float VAD_SILENCE_THRESHOLD = 0.20f;  // порог деактивации (тишина)
    const int SILENCE_HANGOVER_FRAMES = 32;     // ~1.0с тишины подряд -> конец фразы
    const size_t MIN_PHRASE_SAMPLES = static_cast<size_t>(16000 * 0.4f);  // отсечь "чихи"
    const size_t MAX_PHRASE_SAMPLES = static_cast<size_t>(16000 * 15.0f); // страховка от зависшего VAD/шума

    // Антидребезг открытия фразы: одиночный громкий щелчок/хлопок двери/лай
    // собаки может дать один кадр с prob >= порога. Требуем CONFIRM_FRAMES
    // подряд, прежде чем реально считать это началом речи. Едет "с большим
    // числом" - только время открытия фразы, начало слова всё равно не
    // теряется благодаря pre-roll буферу ниже.
    const int CONFIRM_FRAMES = 2; // ~64 мс

    // Примечание про "мышление вслух" (долгая пауза с "эээ" внутри фразы):
    // пока пользователь издаёт хоть какой-то звук выше VAD_SPEECH_THRESHOLD,
    // silence_counter обнуляется сам (см. ветку ниже) - hangover считает
    // только ПОЛНОСТЬЮ тихие паузы. Увеличенный до ~1с hangover даёт чуть
    // больше запаса на короткое молчание при подборе слов.

    // Pre-roll: Silero нужно 2-3 кадра, чтобы "разогнаться" до порога, поэтому
    // начало слова (особенно "Дж..." в "Джарвис") часто обрезается, если писать
    // только с момента срабатывания VAD. Держим кольцевой буфер последних
    // кадров и приклеиваем его к началу фразы при обнаружении речи.
    const size_t PREROLL_FRAMES = 10; // ~320 мс
    std::deque<std::vector<int16_t>> preroll;

    bool is_speaking = false;
    int silence_counter = 0;
    int speech_streak = 0; // подряд идущих кадров выше порога (для антидребезга)
    std::vector<int16_t> speech_buffer;
    double phrase_start_time_unix = 0.0;

    auto NowUnix = []() -> double {
        return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
    };

    std::vector<int16_t> frame;

    while (g_running) {
        if (recorder.ReadFrame(frame)) {
            if (frame.empty()) continue;

            // 1. Громкость кадра (для авто-гейна и статус-бара)
            int16_t max_amp = 0;
            for (int16_t s : frame) {
                max_amp = std::max(max_amp, static_cast<int16_t>(std::abs(s)));
            }

            // 2. Программный авто-гейн перед VAD (Whisper получит НЕ усиленный сигнал,
            //    усиление нужно только чтобы VAD увереннее видел тихую речь/шёпот).
            //    Порог снижен с 100 до 50, потолок усиления поднят с 10x до 15x -
            //    помогает с тихим шёпотом, но не заменяет нормальный уровень
            //    входного сигнала в ОС, если микрофон реально "глухой".
            std::vector<int16_t> boosted_frame = frame;
            if (max_amp > 50 && max_amp < 8000) {
                float gain = 10000.0f / static_cast<float>(max_amp);
                if (gain > 15.0f) gain = 15.0f;
                for (size_t i = 0; i < frame.size(); ++i) {
                    int32_t val = static_cast<int32_t>(frame[i] * gain);
                    boosted_frame[i] = static_cast<int16_t>(std::clamp(val, -32768, 32767));
                }
            }

            // 3. Вероятность речи
            float prob = vad.ProcessFrame(boosted_frame);

            // 4. Поддерживаем pre-roll буфер оригинального (не усиленного) сигнала
            preroll.push_back(frame);
            if (preroll.size() > PREROLL_FRAMES) preroll.pop_front();

            // 5. Конечный автомат
            if (prob >= VAD_SPEECH_THRESHOLD) {
                silence_counter = 0; // хоть какой-то звук выше порога - это не тишина
                if (!is_speaking) {
                    speech_streak++;
                    if (speech_streak >= CONFIRM_FRAMES) {
                        is_speaking = true;
                        speech_streak = 0;
                        speech_buffer.clear();
                        // Приклеиваем pre-roll БЕЗ последнего элемента: preroll уже
                        // содержит текущий кадр (push_back выше по коду в этой же
                        // итерации), а он и так будет добавлен общей веткой
                        // "if (is_speaking)" чуть ниже. Без этой поправки текущий
                        // кадр дублировался бы (32 мс повторяющегося звука ровно
                        // на стыке начала фразы - маленький, но реальный глюк).
                        for (size_t i = 0; i + 1 < preroll.size(); ++i) {
                            speech_buffer.insert(speech_buffer.end(), preroll[i].begin(), preroll[i].end());
                        }
                        // Момент начала фразы отсчитываем от начала pre-roll окна,
                        // а не от текущего кадра - это честнее для задержки STT.
                        phrase_start_time_unix = NowUnix() - (preroll.size() * (512.0 / 16000.0));
                        std::cout << "\n[VAD] >>> Запись фразы..." << std::endl;
                    }
                }
            } else {
                if (!is_speaking) speech_streak = 0; // одиночный щелчок не накопил стрик - сброс
                if (prob < VAD_SILENCE_THRESHOLD && is_speaking) silence_counter++;
            }

            if (is_speaking) {
                speech_buffer.insert(speech_buffer.end(), frame.begin(), frame.end());

                bool silence_timeout = silence_counter >= SILENCE_HANGOVER_FRAMES;
                bool too_long = speech_buffer.size() >= MAX_PHRASE_SAMPLES;

                if (silence_timeout || too_long) {
                    is_speaking = false;
                    silence_counter = 0;

                    if (too_long) {
                        std::cout << "\n[VAD] Фраза слишком длинная, принудительно отправляю "
                                     "и продолжаю слушать (возможно, ложное срабатывание VAD)."
                                  << std::endl;
                    }

                    if (speech_buffer.size() > MIN_PHRASE_SAMPLES) {
                        std::cout << "[ZMQ] Ставлю фразу (" << speech_buffer.size() * sizeof(int16_t)
                                  << " байт) в очередь на отправку..." << std::endl;
                        sender->Enqueue(speech_buffer, phrase_start_time_unix);
                    } else {
                        std::cout << "[VAD] Фраза отфильтрована как слишком короткая." << std::endl;
                    }
                    speech_buffer.clear();

                    // Если фраза была принудительно оборвана по too_long, а человек
                    // продолжает говорить - VAD тут же снова увидит речь на следующем
                    // кадре и откроет новую фразу (is_speaking выставится в true заново
                    // на верхней ветке). Это и есть обработка "перебивания": пользователь
                    // не обязан ждать - речь без паузы просто режется на несколько фраз.
                }
            }

            // Статус-бар
            int bar_len = static_cast<int>(prob * 25.0f);
            std::string bar(bar_len, '#');
            std::string status = is_speaking ? "[ЗАПИСЬ]" : "[ПОИСК] ";

            std::cout << "\r" << status << " | VAD: " << std::setw(5) << std::fixed << std::setprecision(1)
                      << (prob * 100.0f) << "% | Amp: " << std::setw(5) << max_amp
                      << " | " << std::setw(25) << std::left << bar << std::flush;

        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(4));
        }
    }

    std::cout << "\n[Jarvis Core] Завершение работы..." << std::endl;
    sender->Stop();
    recorder.Stop();
    return 0;
}
