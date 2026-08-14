cmake_minimum_required(VERSION 3.12)
project(jarvis_core)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Если тип сборки не указан явно (просто "cmake .."), собираем в Release -
# иначе CMake по умолчанию соберёт без -O2/-O3, а для потоковой обработки
# аудио в реальном времени это заметно на слабом железе.
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# Указываем путь к скачанному ONNX Runtime C++ SDK
set(ONNXRUNTIME_DIR ${CMAKE_CURRENT_SOURCE_DIR}/onnxruntime-linux-x64-1.18.1)

# Поиск системных зависимостей
find_package(PkgConfig REQUIRED)
pkg_check_modules(PORTAUDIO REQUIRED portaudio-2.0)
find_package(cppzmq REQUIRED)

# Исходники (.cpp)
set(SOURCES
    src/main.cpp
    src/AudioRecorder.cpp
    src/SileroVAD.cpp
    src/WakeWordDetector.cpp
)

add_executable(jarvis_core ${SOURCES})

# Заголовочные файлы (.h)
target_include_directories(jarvis_core PRIVATE 
    include
    ${PORTAUDIO_INCLUDE_DIRS}
    ${ONNXRUNTIME_DIR}/include
)

# Линковка библиотек
target_link_libraries(jarvis_core PRIVATE 
    ${PORTAUDIO_LIBRARIES}
    cppzmq
    ${ONNXRUNTIME_DIR}/lib/libonnxruntime.so
)

# RUNPATH относительно расположения бинарника ($ORIGIN), а не абсолютный путь
# сборочной машины. Без этого при переносе/переименовании папки проекта
# бинарник перестаёт находить libonnxruntime.so (то, что уже один раз
# случилось: RUNPATH был захардкожен на /home/artiom/Projects/...).
set_target_properties(jarvis_core PROPERTIES
    BUILD_RPATH "$ORIGIN/../onnxruntime-linux-x64-1.18.1/lib"
    INSTALL_RPATH "$ORIGIN/../onnxruntime-linux-x64-1.18.1/lib"
    BUILD_WITH_INSTALL_RPATH TRUE
)