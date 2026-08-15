#ifndef EMBODIED_AGENT_ROBOT_RUNTIME_H
#define EMBODIED_AGENT_ROBOT_RUNTIME_H

#include <stddef.h>
#include <stdint.h>

#include "robot_protocol.h"

#define ROBOT_UART_RX_CAPACITY 1024U
#define ROBOT_JSON_FRAME_CAPACITY 512U

typedef struct {
    uint8_t bytes[ROBOT_UART_RX_CAPACITY];
    volatile uint16_t head;
    volatile uint16_t tail;
    volatile uint32_t dropped_bytes;
} robot_uart_spsc_ingress_t;

typedef struct {
    char bytes[ROBOT_JSON_FRAME_CAPACITY + 1U];
    size_t length;
    uint8_t dropping_oversize;
} robot_json_frame_t;

typedef int (*robot_runtime_queue_send_fn)(void *context, const robot_command_t *command);
typedef int (*robot_runtime_queue_receive_fn)(void *context, robot_command_t *command);
typedef uint64_t (*robot_runtime_clock_fn)(void *context);
typedef void (*robot_runtime_observation_sink_fn)(
    void *context,
    const robot_observation_t *observation,
    const char *json,
    size_t json_length);

typedef struct {
    robot_runtime_queue_send_fn queue_send;
    robot_runtime_queue_receive_fn queue_receive;
    robot_runtime_clock_fn clock_ms;
    robot_runtime_observation_sink_fn observation_sink;
    void *context;
} robot_runtime_port_t;

typedef struct {
    robot_protocol_t protocol;
    robot_uart_spsc_ingress_t ingress;
    robot_json_frame_t frame;
    robot_runtime_port_t port;
    char observation_json[ROBOT_OBSERVATION_JSON_CAPACITY];
    uint32_t reported_dropped_bytes;
    uint32_t accepted_frames;
    uint32_t rejected_frames;
    uint32_t replayed_results;
    uint32_t executed_commands;
    uint32_t observation_format_failures;
} robot_runtime_t;

int robot_runtime_init(robot_runtime_t *runtime, const robot_runtime_port_t *port);
size_t robot_runtime_uart_rx_isr(
    robot_runtime_t *runtime,
    const uint8_t *bytes,
    size_t length);

/* Process at most one completed ingress event. Returns 1 when progress was emitted/queued. */
int robot_runtime_parser_step(robot_runtime_t *runtime);

/* Execute at most one queued command. Returns 1 when a command was consumed. */
int robot_runtime_control_step(robot_runtime_t *runtime);

size_t robot_uart_spsc_count(const robot_uart_spsc_ingress_t *ingress);

#endif
