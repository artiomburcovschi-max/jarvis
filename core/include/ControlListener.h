#ifndef CONTROL_LISTENER_H
#define CONTROL_LISTENER_H

#include <atomic>
#include <chrono>
#include <iostream>
#include <string>
#include <thread>
#include <zmq.hpp>

class ControlListener {
public:
    ControlListener(zmq::context_t& context, const std::string& endpoint, std::atomic<bool>& muted_flag)
        : socket_(context, ZMQ_PAIR), muted_(muted_flag) {
        socket_.set(zmq::sockopt::rcvtimeo, 500); // мс - чтобы поток мог проверять running_
        socket_.connect(endpoint);
        worker_ = std::thread(&ControlListener::Run, this);
    }

    ~ControlListener() {
        running_ = false;
        if (worker_.joinable()) worker_.join();
    }

private:
    void Run() {
        while (running_) {
            zmq::message_t msg;
            zmq::recv_result_t result;
            try {
                result = socket_.recv(msg, zmq::recv_flags::none);
            } catch (const zmq::error_t& e) {
                std::cerr << "[Control] Ошибка приёма: " << e.what() << std::endl;
                std::this_thread::sleep_for(std::chrono::milliseconds(300));
                continue;
            }

            if (!result) continue; // таймаут - сообщения не было, проверяем running_ и слушаем дальше

            std::string text(static_cast<char*>(msg.data()), msg.size());
            if (text == "MUTE") {
                muted_ = true;
                std::cout << "\n[Control] MUTE (Джарвис говорит - микрофон временно игнорируется)" << std::endl;
            } else if (text == "UNMUTE") {
                muted_ = false;
                std::cout << "\n[Control] UNMUTE (снова слушаю)" << std::endl;
            }
        }
    }

    zmq::socket_t socket_;
    std::atomic<bool>& muted_;
    std::thread worker_;
    std::atomic<bool> running_{true};
};

#endif // CONTROL_LISTENER_H
