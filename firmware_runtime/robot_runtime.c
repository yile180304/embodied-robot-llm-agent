#include "robot_runtime.h"

#include <string.h>

static void copy_text(char *destination, size_t capacity, const char *source)
{
    if (capacity == 0U) return;
    if (source == NULL) {
        destination[0] = '\0';
        return;
    }
    (void)strncpy(destination, source, capacity - 1U);
    destination[capacity - 1U] = '\0';
}

static void make_runtime_observation(
    const robot_runtime_t *runtime,
    const robot_command_t *command,
    robot_status_t status,
    uint64_t now_ms,
    const char *error_code,
    const char *error_message,
    robot_observation_t *observation)
{
    memset(observation, 0, sizeof(*observation));
    if (command == NULL) {
        copy_text(observation->task_id, sizeof(observation->task_id), "invalid-command");
        observation->seq = 1U;
    } else {
        copy_text(observation->task_id, sizeof(observation->task_id), command->task_id);
        observation->seq = command->seq;
    }
    observation->status = status;
    observation->state = runtime->protocol.state;
    copy_text(observation->error_code, sizeof(observation->error_code), error_code);
    copy_text(observation->error_message, sizeof(observation->error_message), error_message);
    observation->received_at_ms = now_ms;
}

static int emit_observation(robot_runtime_t *runtime, const robot_observation_t *observation)
{
    size_t written = 0U;
    if (!robot_observation_to_json(
            observation,
            runtime->observation_json,
            sizeof(runtime->observation_json),
            &written)) {
        runtime->observation_format_failures++;
        return 0;
    }
    runtime->port.observation_sink(
        runtime->port.context,
        observation,
        runtime->observation_json,
        written);
    return 1;
}

static int ingress_pop(robot_uart_spsc_ingress_t *ingress, uint8_t *byte)
{
    uint16_t tail;
    if (ingress == NULL || byte == NULL) return 0;
    tail = ingress->tail;
    if (tail == ingress->head) return 0;
    *byte = ingress->bytes[tail];
    ingress->tail = (uint16_t)((tail + 1U) % ROBOT_UART_RX_CAPACITY);
    return 1;
}

static void flush_ingress(robot_uart_spsc_ingress_t *ingress)
{
    ingress->tail = ingress->head;
}

static int process_frame(robot_runtime_t *runtime, uint64_t now_ms)
{
    robot_ingest_result_t result;
    robot_observation_t observation;
    robot_status_t status = robot_protocol_prepare_json(
        &runtime->protocol,
        runtime->frame.bytes,
        now_ms,
        &result);
    runtime->frame.length = 0U;
    runtime->frame.bytes[0] = '\0';

    if (result.disposition == ROBOT_INGEST_ENQUEUE) {
        if (!runtime->port.queue_send(runtime->port.context, &result.command)) {
            make_runtime_observation(
                runtime,
                &result.command,
                ROBOT_STATUS_QUEUE_FULL,
                now_ms,
                "queue_full",
                "command queue has no free slot",
                &observation);
            runtime->rejected_frames++;
            (void)emit_observation(runtime, &observation);
            return 1;
        }
        (void)robot_protocol_commit_pending(&runtime->protocol, &result.command);
        runtime->accepted_frames++;
        return 1;
    }
    if (result.disposition == ROBOT_INGEST_REPLAY) {
        runtime->replayed_results++;
        (void)emit_observation(runtime, &result.observation);
        return 1;
    }
    if (result.disposition == ROBOT_INGEST_PENDING) {
        return 1;
    }
    (void)status;
    runtime->rejected_frames++;
    (void)emit_observation(runtime, &result.observation);
    return 1;
}

int robot_runtime_init(robot_runtime_t *runtime, const robot_runtime_port_t *port)
{
    if (runtime == NULL || port == NULL || port->queue_send == NULL ||
        port->queue_receive == NULL || port->clock_ms == NULL ||
        port->observation_sink == NULL) return 0;
    memset(runtime, 0, sizeof(*runtime));
    runtime->port = *port;
    robot_protocol_init(&runtime->protocol);
    return 1;
}

size_t robot_runtime_uart_rx_isr(
    robot_runtime_t *runtime,
    const uint8_t *bytes,
    size_t length)
{
    size_t i;
    size_t accepted = 0U;
    if (runtime == NULL || bytes == NULL) return 0U;
    for (i = 0U; i < length; ++i) {
        robot_uart_spsc_ingress_t *ingress = &runtime->ingress;
        const uint16_t head = ingress->head;
        const uint16_t next = (uint16_t)((head + 1U) % ROBOT_UART_RX_CAPACITY);
        if (next == ingress->tail) {
            ingress->dropped_bytes++;
            continue;
        }
        ingress->bytes[head] = bytes[i];
        ingress->head = next;
        accepted++;
    }
    return accepted;
}

int robot_runtime_parser_step(robot_runtime_t *runtime)
{
    uint8_t byte;
    uint64_t now_ms;
    robot_observation_t observation;
    uint32_t dropped;
    if (runtime == NULL) return 0;
    now_ms = runtime->port.clock_ms(runtime->port.context);
    dropped = runtime->ingress.dropped_bytes;
    if (dropped != runtime->reported_dropped_bytes) {
        runtime->reported_dropped_bytes = dropped;
        flush_ingress(&runtime->ingress);
        runtime->frame.length = 0U;
        runtime->frame.dropping_oversize = 0U;
        make_runtime_observation(
            runtime,
            NULL,
            ROBOT_STATUS_REJECTED,
            now_ms,
            "uart_overflow",
            "UART ingress overflowed; partial bytes were discarded",
            &observation);
        runtime->rejected_frames++;
        (void)emit_observation(runtime, &observation);
        return 1;
    }

    while (ingress_pop(&runtime->ingress, &byte)) {
        if (runtime->frame.dropping_oversize) {
            if (byte == (uint8_t)'\n') {
                runtime->frame.dropping_oversize = 0U;
                runtime->frame.length = 0U;
                make_runtime_observation(
                    runtime,
                    NULL,
                    ROBOT_STATUS_REJECTED,
                    now_ms,
                    "frame_too_long",
                    "JSON frame exceeded the fixed capacity",
                    &observation);
                runtime->rejected_frames++;
                (void)emit_observation(runtime, &observation);
                return 1;
            }
            continue;
        }
        if (byte == (uint8_t)'\r') continue;
        if (byte == (uint8_t)'\n') {
            if (runtime->frame.length == 0U) continue;
            runtime->frame.bytes[runtime->frame.length] = '\0';
            return process_frame(runtime, now_ms);
        }
        if (runtime->frame.length >= ROBOT_JSON_FRAME_CAPACITY) {
            runtime->frame.length = 0U;
            runtime->frame.dropping_oversize = 1U;
            continue;
        }
        runtime->frame.bytes[runtime->frame.length++] = (char)byte;
    }
    return 0;
}

int robot_runtime_control_step(robot_runtime_t *runtime)
{
    robot_command_t command;
    robot_observation_t observation;
    uint64_t now_ms;
    if (runtime == NULL ||
        !runtime->port.queue_receive(runtime->port.context, &command)) return 0;
    now_ms = runtime->port.clock_ms(runtime->port.context);
    (void)robot_protocol_execute_command(
        &runtime->protocol,
        &command,
        now_ms,
        &observation);
    runtime->executed_commands++;
    (void)emit_observation(runtime, &observation);
    return 1;
}

size_t robot_uart_spsc_count(const robot_uart_spsc_ingress_t *ingress)
{
    uint16_t head;
    uint16_t tail;
    if (ingress == NULL) return 0U;
    head = ingress->head;
    tail = ingress->tail;
    if (head >= tail) return (size_t)(head - tail);
    return (size_t)(ROBOT_UART_RX_CAPACITY - (size_t)tail + (size_t)head);
}
