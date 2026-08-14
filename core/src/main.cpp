#include <iostream>
#include <string>
#include <cstdlib>
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
#include "WakeWordDetector.h"
#include "PhraseSender.h"
#include "ControlListener.h"

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

    // Раунд 14 (B2): отдельный wake-word детектор - см. WakeWordDetector.h.
    // ПОЛНОСТЬЮ ОПЦИОНАЛЕН: пока не заданы все три переменные окружения
    // (или пока нет реальной обученной модели под нужное слово - для
    // русского "Джарвис" такой модели пока НЕТ, см. README.md, раздел
    // "Раунд 14"), детектор молча выключен и ядро работает РОВНО как раньше
    // (Whisper + fuzzy-поиск на стороне Python). Задать пути можно так:
    //   WAKEWORD_MELSPEC_MODEL=../models/melspectrogram.onnx
    //   WAKEWORD_EMBEDDING_MODEL=../models/embedding_model.onnx
    //   WAKEWORD_MODEL=../models/jarvis_ru.onnx
    auto GetEnvOrEmpty = [](const char* name) -> std::string {
        const char* value = std::getenv(name);
        return value ? std::string(value) : std::string();
    };
    WakeWordDetector wake_word_detector(
        GetEnvOrEmpty("WAKEWORD_MELSPEC_MODEL"),
        GetEnvOrEmpty("WAKEWORD_EMBEDDING_MODEL"),
        GetEnvOrEmpty("WAKEWORD_MODEL")
    );
    // Порог/гистерезис срабатывания - настраиваются через переменные
    // окружения, т.к. под РАЗНЫЕ модели (и разные комнаты/микрофоны)
    // разумный порог отличается. Например, у англоязычной тестовой модели
    // "hey_jarvis" даже на правильно произнесённой фразе скор держится в
    // районе 0.35-0.4 (см. README.md, "Раунд 14") - для неё порог 0.5 по
    // умолчанию слишком строгий, есть смысл понизить до ~0.3 через
    // WAKEWORD_THRESHOLD=0.3.
    auto GetEnvFloatOrDefault = [](const char* name, float default_value) -> float {
        const char* value = std::getenv(name);
        return value ? std::stof(value) : default_value;
    };
    auto GetEnvIntOrDefault = [](const char* name, int default_value) -> int {
        const char* value = std::getenv(name);
        return value ? std::atoi(value) : default_value;
    };
    const float WAKEWORD_THRESHOLD = GetEnvFloatOrDefault("WAKEWORD_THRESHOLD", 0.5f);
    const int WAKEWORD_TRIGGER_LEVEL = GetEnvIntOrDefault("WAKEWORD_TRIGGER_LEVEL", 3);   // подряд идущих кадров >= порога
    const int WAKEWORD_REFRACTORY_FRAMES = GetEnvIntOrDefault("WAKEWORD_REFRACTORY_FRAMES", 60);  // ~2с "остывания" после срабатывания (кадр ~32мс)
    int wakeword_streak = 0;
    int wakeword_refractory_counter = 0;

    // MUTE/UNMUTE от Python вокруг воспроизведения TTS-ответа - грубый
    // полудуплекс, чтобы микрофон не подхватывал собственный голос Джарвиса
    // из динамика (полноценное эхоподавление - отдельная задача на будущее).
    std::atomic<bool> is_muted{false};
    ControlListener control_listener(zmq_context, "tcp://127.0.0.1:5557", is_muted);

    if (!recorder.Start()) {
        std::cerr << "[ОШИБКА] Не удалось запустить запись с микрофона!" << std::endl;
        return 1;
    }

    std::cout << "\n[Jarvis Core] Готов! Говори в микрофон... (Ctrl+C для выхода)\n" << std::endl;

    // --- Настройки конечного автомата VAD ---
    const float VAD_SPEECH_THRESHOLD = 0.55f;   // порог активации (речь) в обычном режиме
    const float VAD_SILENCE_THRESHOLD = 0.20f;  // порог деактивации (тишина)
    const int SILENCE_HANGOVER_FRAMES = 32;     // ~1.0с тишины подряд -> конец фразы
    const size_t MIN_PHRASE_SAMPLES = static_cast<size_t>(16000 * 0.4f);  // отсечь "чихи"
    const size_t MAX_PHRASE_SAMPLES = static_cast<size_t>(16000 * 15.0f); // страховка от зависшего VAD/шума

    // Раунд 10 (прерывание/barge-in): пока Джарвис говорит (is_muted от
    // Python - см. ControlListener), микрофон больше НЕ глушится полностью -
    // вместо этого используется более строгий порог и более длинный
    // антидребезг. Смысл: собственный голос Джарвиса, просочившийся из
    // динамика в микрофон, обычно тише и "смазаннее", чем настоящая речь
    // пользователя прямо в микрофон - завышенный порог отсеивает такую
    // самоперекличку, а настоящий уверенный голос пользователя (особенно
    // если микрофон и колонка физически разнесены, как планируется) всё
    // равно её пробивает и прерывает ответ. Это НЕ полноценное
    // эхоподавление (AEC) - компромисс, а не безупречное решение.
    const float VAD_SPEECH_THRESHOLD_INTERRUPT = 0.85f;
    const int CONFIRM_FRAMES_INTERRUPT = 5; // ~160мс уверенной речи, не короткий всплеск

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
    bool was_muted = false;

    while (g_running) {
        if (recorder.ReadFrame(frame)) {
            if (frame.empty()) continue;

            // 0. Раунд 10: пока Джарвис говорит (jarvis_speaking), микрофон
            //    НЕ отключается - используется более строгий порог
            //    (VAD_SPEECH_THRESHOLD_INTERRUPT/CONFIRM_FRAMES_INTERRUPT)
            //    вместо обычного. Так пользователь может реально перебить
            //    ответ уверенным голосом, а тихая перекличка динамика в
            //    микрофон отсеивается более высоким порогом.
            bool jarvis_speaking = is_muted.load();
            if (jarvis_speaking && !was_muted && is_speaking) {
                // Джарвис только что начал говорить, а у нас в этот момент
                // уже шла запись фразы - обрываем её, не отправляя (в неё
                // уже могла попасть перекличка TTS на стыке).
                std::cout << "\n[VAD] Фраза оборвана - Джарвис начал говорить." << std::endl;
                is_speaking = false;
                silence_counter = 0;
                speech_streak = 0;
                speech_buffer.clear();
            }
            was_muted = jarvis_speaking;

            const float active_speech_threshold = jarvis_speaking ? VAD_SPEECH_THRESHOLD_INTERRUPT : VAD_SPEECH_THRESHOLD;
            const int active_confirm_frames = jarvis_speaking ? CONFIRM_FRAMES_INTERRUPT : CONFIRM_FRAMES;

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

            // 3.5. Раунд 14 (B2): отдельный wake-word детектор - кормим СЫРЫМ
            //      (не усиленным автогейном) кадром, как и то, что в итоге
            //      уходит в Whisper. Работает НЕЗАВИСИМО от VAD-конечного
            //      автомата ниже - если модель не задана (см. выше), просто
            //      всегда возвращает -1.0f и ничего не делает (no-op).
            float wakeword_score = wake_word_detector.ProcessFrame(frame);
            if (wakeword_refractory_counter > 0) wakeword_refractory_counter--;

            // 4. Поддерживаем pre-roll буфер оригинального (не усиленного) сигнала
            preroll.push_back(frame);
            if (preroll.size() > PREROLL_FRAMES) preroll.pop_front();

            // Начало фразы - общая логика и для обычного VAD-порога, и для
            // срабатывания wake-word (см. ниже) - вынесено в лямбду, чтобы
            // не дублировать "приклеить pre-roll + отсчитать честное время
            // начала фразы" в двух местах.
            auto BeginPhrase = [&]() {
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

                // Раунд 22 (B3): мгновенный сигнал в Python/UI - "слушаю
                // вас" - ещё ДО того, как фраза целиком закончится и уйдёт
                // на распознавание (это может занимать секунды). Дёшево:
                // всего пара десятков байт по уже открытому соединению,
                // никакой дополнительной обработки аудио.
                sender->EnqueueSignal("speech_started", phrase_start_time_unix);
            };

            // Сработал wake-word (гистерезис: WAKEWORD_TRIGGER_LEVEL кадров
            // подряд выше порога, не единичный всплеск) - НЕ ждём антидребезг
            // обычного VAD (CONFIRM_FRAMES): акустическое подтверждение уже
            // сильное само по себе, слово короткое, и лишняя задержка здесь -
            // это именно та задержка, ради устранения которой весь B2 и
            // затевался (см. README.md, раздел "Раунд 14").
            if (wake_word_detector.IsEnabled() && wakeword_refractory_counter == 0) {
                if (wakeword_score >= WAKEWORD_THRESHOLD) {
                    wakeword_streak++;
                    if (wakeword_streak >= WAKEWORD_TRIGGER_LEVEL && !is_speaking) {
                        std::cout << "\n[WakeWord] Активационное слово обнаружено (score="
                                  << wakeword_score << ")!" << std::endl;
                        BeginPhrase();
                        wakeword_streak = 0;
                        wakeword_refractory_counter = WAKEWORD_REFRACTORY_FRAMES;
                    }
                } else {
                    wakeword_streak = 0;
                }
            }

            // 5. Конечный автомат
            if (prob >= active_speech_threshold) {
                silence_counter = 0; // хоть какой-то звук выше порога - это не тишина
                if (!is_speaking) {
                    speech_streak++;
                    if (speech_streak >= active_confirm_frames) {
                        BeginPhrase();
                        std::cout << "\n[VAD] >>> Запись фразы"
                                  << (jarvis_speaking ? " (ПРЕРЫВАНИЕ)..." : "...") << std::endl;
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
                        // Раунд 22 (B3): без этого сигнала UI застряла бы в
                        // "слушаю вас" навсегда после случайного щелчка/кашля -
                        // "speech_started" уже отправили, а полной фразы вслед
                        // за ним не будет (см. комментарий у EnqueueSignal).
                        sender->EnqueueSignal("speech_discarded", phrase_start_time_unix);
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
            std::string status = is_speaking ? "[ЗАПИСЬ]" : (jarvis_speaking ? "[ГОВОРИТ]" : "[ПОИСК] ");

            std::cout << "\r" << status << " | VAD: " << std::setw(5) << std::fixed << std::setprecision(1)
                      << (prob * 100.0f) << "% (порог " << std::setprecision(0)
                      << (active_speech_threshold * 100.0f) << "%) | Amp: " << std::setw(5) << max_amp;
            if (wake_word_detector.IsEnabled()) {
                // Только если детектор реально включён (см. выше) - иначе
                // wakeword_score всегда -1.0f и просто загромождал бы строку.
                std::cout << " | WW: " << std::setw(5) << std::setprecision(2)
                          << std::max(0.0f, wakeword_score);
            }
            std::cout << " | " << std::setw(25) << std::left << bar << std::flush;

        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(4));
        }
    }

    std::cout << "\n[Jarvis Core] Завершение работы..." << std::endl;
    sender->Stop();
    recorder.Stop();
    return 0;
}
