#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "robot_runtime_host.h"

static const char *move_frame(const char *task_id, unsigned seq)
{
    static char frame[512];
    (void)snprintf(frame, sizeof(frame),
                   "{\"version\":1,\"task_id\":\"%s\",\"seq\":%u,"
                   "\"tool\":\"move_robot\",\"params\":{\"distance_m\":1.0,"
                   "\"speed_mps\":0.2},\"deadline_ms\":10000,\"sent_at_ms\":1000}\n",
                   task_id, seq);
    return frame;
}

static const char *state_frame(const char *task_id, unsigned seq)
{
    static char frame[384];
    (void)snprintf(frame, sizeof(frame),
                   "{\"version\":1,\"task_id\":\"%s\",\"seq\":%u,"
                   "\"tool\":\"get_robot_state\",\"params\":{},"
                   "\"deadline_ms\":10000,\"sent_at_ms\":1000}\n",
                   task_id, seq);
    return frame;
}

static const char *turn_frame(const char *task_id, unsigned seq)
{
    static char frame[384];
    (void)snprintf(frame, sizeof(frame),
                   "{\"version\":1,\"task_id\":\"%s\",\"seq\":%u,"
                   "\"tool\":\"turn_robot\",\"params\":{\"angle_deg\":90,"
                   "\"angular_speed_dps\":45},\"deadline_ms\":10000,\"sent_at_ms\":1000}\n",
                   task_id, seq);
    return frame;
}

static const char *stop_frame(const char *task_id, unsigned seq)
{
    static char frame[384];
    (void)snprintf(frame, sizeof(frame),
                   "{\"version\":1,\"task_id\":\"%s\",\"seq\":%u,"
                   "\"tool\":\"emergency_stop\",\"params\":{\"reason\":\"tilt\"},"
                   "\"deadline_ms\":10000,\"sent_at_ms\":1000}\n",
                   task_id, seq);
    return frame;
}

static void assert_valid_observation_json(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    assert(root != NULL);
    assert(cJSON_GetObjectItem(root, "version") != NULL);
    assert(cJSON_GetObjectItem(root, "task_id") != NULL);
    assert(cJSON_GetObjectItem(root, "seq") != NULL);
    assert(cJSON_GetObjectItem(root, "status") != NULL);
    assert(cJSON_GetObjectItem(root, "observation") != NULL);
    assert(cJSON_GetObjectItem(root, "received_at_ms") != NULL);
    cJSON_Delete(root);
}

int main(void)
{
    robot_runtime_host_t host;
    robot_runtime_host_t queue_full;
    robot_runtime_host_t framing;
    robot_runtime_host_t overflow;
    const char *frame;
    size_t frame_length;
    size_t split;
    size_t i;
    char two_frames[768];
    char oversized[900];
    char overflow_bytes[ROBOT_UART_RX_CAPACITY + 64U];

    assert(robot_runtime_host_init(&host, 4U) == 1);
    robot_runtime_host_set_time(&host, 1001U);
    frame = move_frame("runtime", 1U);
    frame_length = strlen(frame);
    split = 23U;
    assert(robot_runtime_uart_rx_isr(
               &host.runtime, (const uint8_t *)frame, split) == split);
    assert(robot_runtime_host_drain_parser(&host) == 0U);
    assert(robot_runtime_host_queue_count(&host) == 0U);
    assert(robot_runtime_uart_rx_isr(
               &host.runtime,
               (const uint8_t *)(frame + split),
               frame_length - split) == frame_length - split);
    assert(robot_runtime_host_drain_parser(&host) == 1U);
    assert(robot_runtime_host_queue_count(&host) == 1U);

    assert(robot_runtime_host_feed(&host, frame) == frame_length);
    assert(robot_runtime_host_drain_parser(&host) == 1U);
    assert(robot_runtime_host_queue_count(&host) == 1U);
    assert(host.observation_count == 0U);

    robot_runtime_host_set_time(&host, 1002U);
    assert(robot_runtime_host_drain_control(&host) == 1U);
    assert(host.observation_count == 1U);
    assert(host.observations[0].status == ROBOT_STATUS_BLOCKED);
    assert(host.runtime.protocol.state.executed_command_count == 0U);

    assert(robot_runtime_host_feed(&host, frame) == frame_length);
    assert(robot_runtime_host_drain_parser(&host) == 1U);
    assert(host.observation_count == 2U);
    assert(host.observations[1].received_at_ms == host.observations[0].received_at_ms);
    assert(host.runtime.protocol.state.executed_command_count == 0U);
    assert(host.runtime.replayed_results == 1U);

    assert(robot_protocol_set_obstacles(&host.runtime.protocol, 100.0f, 120.0f, 35.0f) == 1);
    (void)snprintf(two_frames, sizeof(two_frames), "%s%s", turn_frame("runtime", 2U),
                   move_frame("runtime", 3U));
    assert(robot_runtime_host_feed(&host, two_frames) == strlen(two_frames));
    assert(robot_runtime_host_drain_parser(&host) == 2U);
    assert(robot_runtime_host_queue_count(&host) == 2U);
    robot_runtime_host_set_time(&host, 1004U);
    assert(robot_runtime_host_drain_control(&host) == 2U);
    assert(host.observations[2].status == ROBOT_STATUS_SUCCESS);
    assert(host.observations[3].status == ROBOT_STATUS_SUCCESS);
    assert(host.runtime.protocol.state.y_m > 0.99f && host.runtime.protocol.state.y_m < 1.01f);
    assert(host.runtime.protocol.state.executed_command_count == 2U);

    frame = move_frame("runtime", 3U);
    assert(robot_runtime_host_feed(&host, frame) == strlen(frame));
    assert(robot_runtime_host_drain_parser(&host) == 1U);
    assert(host.observation_count == 5U);
    assert(host.runtime.protocol.state.executed_command_count == 2U);
    assert(host.runtime.protocol.state.y_m > 0.99f && host.runtime.protocol.state.y_m < 1.01f);

    frame = stop_frame("runtime", 4U);
    assert(robot_runtime_host_feed(&host, frame) == strlen(frame));
    assert(robot_runtime_host_drain_parser(&host) == 1U);
    assert(robot_runtime_host_drain_control(&host) == 1U);
    assert(host.observations[5].status == ROBOT_STATUS_EMERGENCY_STOP);
    frame = move_frame("runtime", 5U);
    assert(robot_runtime_host_feed(&host, frame) == strlen(frame));
    assert(robot_runtime_host_drain_parser(&host) == 1U);
    assert(robot_runtime_host_drain_control(&host) == 1U);
    assert(host.observations[6].status == ROBOT_STATUS_REJECTED);
    assert(strcmp(host.observations[6].error_code, "emergency_stopped") == 0);

    assert(robot_runtime_host_init(&queue_full, 1U) == 1);
    robot_runtime_host_set_time(&queue_full, 1001U);
    (void)strcpy(two_frames, state_frame("full", 1U));
    (void)strcat(two_frames, state_frame("full", 2U));
    assert(robot_runtime_host_feed(&queue_full, two_frames) == strlen(two_frames));
    assert(robot_runtime_host_drain_parser(&queue_full) == 2U);
    assert(robot_runtime_host_queue_count(&queue_full) == 1U);
    assert(queue_full.observation_count == 1U);
    assert(strcmp(queue_full.observations[0].error_code, "queue_full") == 0);
    assert(robot_runtime_host_drain_control(&queue_full) == 1U);
    frame = state_frame("full", 2U);
    assert(robot_runtime_host_feed(&queue_full, frame) == strlen(frame));
    assert(robot_runtime_host_drain_parser(&queue_full) == 1U);
    assert(robot_runtime_host_drain_control(&queue_full) == 1U);
    assert(queue_full.observations[2].status == ROBOT_STATUS_SUCCESS);

    assert(robot_runtime_host_init(&framing, 4U) == 1);
    robot_runtime_host_set_time(&framing, 1001U);
    memset(oversized, 'x', 700U);
    oversized[700] = '\n';
    (void)strcpy(oversized + 701U, state_frame("recover", 1U));
    assert(robot_runtime_uart_rx_isr(
               &framing.runtime,
               (const uint8_t *)oversized,
               strlen(oversized)) == strlen(oversized));
    assert(robot_runtime_host_drain_parser(&framing) == 2U);
    assert(strcmp(framing.observations[0].error_code, "frame_too_long") == 0);
    assert(robot_runtime_host_drain_control(&framing) == 1U);
    assert(framing.observations[1].status == ROBOT_STATUS_SUCCESS);

    assert(robot_runtime_host_feed(&framing, "{}\n") == 3U);
    assert(robot_runtime_host_drain_parser(&framing) == 1U);
    assert(strcmp(framing.observations[2].error_code, "schema_validation_error") == 0);

    assert(robot_runtime_host_init(&overflow, 4U) == 1);
    robot_runtime_host_set_time(&overflow, 1001U);
    memset(overflow_bytes, 'z', sizeof(overflow_bytes));
    assert(robot_runtime_uart_rx_isr(
               &overflow.runtime,
               (const uint8_t *)overflow_bytes,
               sizeof(overflow_bytes)) == ROBOT_UART_RX_CAPACITY - 1U);
    assert(overflow.runtime.ingress.dropped_bytes > 0U);
    assert(robot_runtime_host_drain_parser(&overflow) == 1U);
    assert(strcmp(overflow.observations[0].error_code, "uart_overflow") == 0);
    assert(robot_uart_spsc_count(&overflow.runtime.ingress) == 0U);
    frame = state_frame("overflow", 1U);
    assert(robot_runtime_host_feed(&overflow, frame) == strlen(frame));
    assert(robot_runtime_host_drain_parser(&overflow) == 1U);
    assert(robot_runtime_host_drain_control(&overflow) == 1U);
    assert(overflow.observations[1].status == ROBOT_STATUS_SUCCESS);

    for (i = 0U; i < host.observation_count; ++i) {
        assert_valid_observation_json(host.observation_json[i]);
        (void)puts(host.observation_json[i]);
    }
    for (i = 0U; i < queue_full.observation_count; ++i) {
        assert_valid_observation_json(queue_full.observation_json[i]);
        (void)puts(queue_full.observation_json[i]);
    }
    for (i = 0U; i < framing.observation_count; ++i) {
        assert_valid_observation_json(framing.observation_json[i]);
        (void)puts(framing.observation_json[i]);
    }
    for (i = 0U; i < overflow.observation_count; ++i) {
        assert_valid_observation_json(overflow.observation_json[i]);
        (void)puts(overflow.observation_json[i]);
    }
    return 0;
}
