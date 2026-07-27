#ifndef PHRASE_SENDER_H
#define PHRASE_SENDER_H

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <iostream>
#include <mutex>
#include <sstream>
#include <thread>
#include <vector>
#include <zmq.hpp>

class PhraseSender {
public:
    PhraseSender(zmq::context_t& context, const std::string& endpoint)
        : socket_(context, ZMQ_PAIR) {
        socket_.connect(endpoint);
        worker_ = std::thread(&PhraseSender::Run, this);
    }

    ~PhraseSender() {
        Stop();
    }

    void Enqueue(std::vector<int16_t> phrase, double capture_time_unix) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (queue_.size() >= kMaxQueueSize) {
                std::cerr << "[PhraseSender] Очередь переполнена (" << kMaxQueueSize
                          << " фраз) - Python недоступен слишком долго, "
                             "старейшая фраза в очереди отброшена." << std::endl;
                queue_.pop_front();
            }
            queue_.push_back({std::move(phrase), capture_time_unix, next_seq_++});
        }
        cv_.notify_one();
    }

    void Stop() {
        if (!running_) return;
        running_ = false;
        cv_.notify_all();
        if (worker_.joinable()) worker_.join();
    }

private:
    struct QueuedPhrase {
        std::vector<int16_t> samples;
        double capture_time_unix;
        uint64_t seq;
    };

    void Run() {
        while (running_) {
            QueuedPhrase item;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                cv_.wait(lock, [this] { return !queue_.empty() || !running_; });
                if (!running_ && queue_.empty()) return;
                if (queue_.empty()) continue;
                item = std::move(queue_.front());
                queue_.pop_front();
            }

            std::ostringstream header;
            header << "{\"seq\":" << item.seq << ",\"ts\":" << std::fixed << item.capture_time_unix << "}";
            std::string header_str = header.str();

            size_t bytes_size = item.samples.size() * sizeof(int16_t);

            try {
                zmq::message_t header_msg(header_str.data(), header_str.size());
                socket_.send(header_msg, zmq::send_flags::sndmore);

                zmq::message_t audio_msg(bytes_size);
                std::memcpy(audio_msg.data(), item.samples.data(), bytes_size);
                socket_.send(audio_msg, zmq::send_flags::none);

                std::cout << "[ZMQ] Фраза #" << item.seq << " отправлена (" << bytes_size
                          << " байт, в очереди ещё " << QueueSize() << ")" << std::endl;
            } catch (const zmq::error_t& e) {
                std::cerr << "[ZMQ] Ошибка отправки фразы #" << item.seq << ": " << e.what() << std::endl;
                // Не теряем фразу молча: кладём обратно в начало очереди и пробуем
                // ещё раз чуть позже (например, Python-сервер ещё не успел
                // забиндиться при старте, или произошёл кратковременный сбой).
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    queue_.push_front(std::move(item));
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(300));
            }
        }
    }

    size_t QueueSize() {
        std::lock_guard<std::mutex> lock(mutex_);
        return queue_.size();
    }

    zmq::socket_t socket_;
    std::thread worker_;
    std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<QueuedPhrase> queue_;
    std::atomic<bool> running_{true};
    uint64_t next_seq_{0};
    static constexpr size_t kMaxQueueSize = 50; // ~несколько минут речи про запас
};

#endif // PHRASE_SENDER_H
