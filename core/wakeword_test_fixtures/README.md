# Тестовый набор для проверки WakeWordDetector (sanity-check)

Эти файлы НЕ для продакшена - это готовый набор для проверки, что сам
конвейер (melspectrogram -> embedding -> классификатор) работает правильно
на вашей машине, ДО того как разбираться с настоящей моделью под "Джарвис".

- `melspectrogram.onnx`, `embedding_model.onnx` - официальные общие модели
  openWakeWord (Apache-2.0), скачаны с
  https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/
  Эти два файла НЕ зависят от конкретного слова-активатора - используются
  для ЛЮБОЙ модели, включая будущую русскую.
- `hey_jarvis_v0.1.onnx` - готовая англоязычная модель слова "hey jarvis"
  (та же community-модель, что в примерах openWakeWord). НЕ распознает
  русское произношение "Джарвис" - только для проверки механики.
- `*_16k.wav` - тестовые фразы (синтезированы espeak-ng, английский голос,
  ресемплированы в 16kHz/mono/PCM16, как и ожидает конвейер).

## Как проверить

```bash
cd core
g++ -std=c++17 -O2 -I include -I onnxruntime-linux-x64-1.18.1/include \
    src/WakeWordDetector.cpp test_wakeword_standalone.cpp \
    -o /tmp/test_wakeword \
    -L onnxruntime-linux-x64-1.18.1/lib -lonnxruntime \
    -Wl,-rpath,onnxruntime-linux-x64-1.18.1/lib

LD_LIBRARY_PATH=onnxruntime-linux-x64-1.18.1/lib /tmp/test_wakeword \
    wakeword_test_fixtures/melspectrogram.onnx \
    wakeword_test_fixtures/embedding_model.onnx \
    wakeword_test_fixtures/hey_jarvis_v0.1.onnx \
    wakeword_test_fixtures/hey_jarvis_16k.wav \
    wakeword_test_fixtures/control_16k.wav \
    wakeword_test_fixtures/hey_jarvis_sentence_16k.wav
```

Ожидаемый результат (проверено при разработке, см. README.md проекта,
раздел "Раунд 14"):
- `hey_jarvis_16k.wav` - предсказаний мало/нет (клип слишком короткий, буфер
  не успевает прогреться - это нормально, не баг).
- `control_16k.wav` (посторонняя фраза) - максимум скора в районе 0.0001,
  то есть практически ноль.
- `hey_jarvis_sentence_16k.wav` ("hey jarvis, what is the weather like
  today") - чёткий всплеск скора **около 0.387** ровно в районе, где
  произнесено "jarvis" - на порядки выше, чем в контрольной фразе.

Если у вас получаются числа ДРУГОГО порядка (не единицы/десятые, а,
например, всегда ровно 0 или NaN) - значит что-то не так со сборкой/путями,
а не с самой моделью - есть смысл разбираться до того, как подключать
настоящую (русскую) модель, чтобы не путать "не работает конвейер" с
"модель не распознаёт произношение".
