cmake_minimum_required(VERSION 3.12)
project(jarvis_core)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

set(ONNXRUNTIME_DIR ${CMAKE_CURRENT_SOURCE_DIR}/onnxruntime-linux-x64-1.18.1)

find_package(PkgConfig REQUIRED)
pkg_check_modules(PORTAUDIO REQUIRED portaudio-2.0)
find_package(cppzmq REQUIRED)

set(SOURCES
    src/main.cpp
    src/AudioRecorder.cpp
    src/SileroVAD.cpp
)

add_executable(jarvis_core ${SOURCES})

target_include_directories(jarvis_core PRIVATE 
    include
    ${PORTAUDIO_INCLUDE_DIRS}
    ${ONNXRUNTIME_DIR}/include
)

target_link_libraries(jarvis_core PRIVATE 
    ${PORTAUDIO_LIBRARIES}
    cppzmq
    ${ONNXRUNTIME_DIR}/lib/libonnxruntime.so
)

set_target_properties(jarvis_core PROPERTIES
    BUILD_RPATH "$ORIGIN/../onnxruntime-linux-x64-1.18.1/lib"
    INSTALL_RPATH "$ORIGIN/../onnxruntime-linux-x64-1.18.1/lib"
    BUILD_WITH_INSTALL_RPATH TRUE
)
