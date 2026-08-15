#ifndef EMBODIED_AGENT_ROBOT_RUNTIME_HOST_H
#define EMBODIED_AGENT_ROBOT_RUNTIME_HOST_H

#include "robot_runtime.h"

#define ROBOT_HOST_QUEUE_CAPACITY 8U
#define ROBOT_HOST_OBSERVATION_CAPACITY 64U

typedef struct {
    robot_runtime_t runtime;
    robot_command_t queue[ROBOT_HOST_QUEUE_CAPACITY];
    size_t queue_head;
    size_t queue_tail;
    size_t queue_count;
    size_t queue_capacity;
    uint64_t now_ms;
    robot_observation_t observations[ROBOT_HOST_OBSERVATION_CAPACITY];
    char observation_json[ROBOT_HOST_OBSERVATION_CAPACITY][ROBOT_OBSERVATION_JSON_CAPACITY];
    size_t observation_count;
} robot_runtime_host_t;

int robot_runtime_host_init(robot_runtime_host_t *host, size_t queue_capacity);
void robot_runtime_host_set_time(robot_runtime_host_t *host, uint64_t now_ms);
size_t robot_runtime_host_feed(robot_runtime_host_t *host, const char *bytes);
size_t robot_runtime_host_drain_parser(robot_runtime_host_t *host);
size_t robot_runtime_host_drain_control(robot_runtime_host_t *host);
size_t robot_runtime_host_queue_count(const robot_runtime_host_t *host);

#endif
