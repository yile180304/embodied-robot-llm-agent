#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "robot_protocol.h"

static const char *move_json(
    const char *task_id,
    unsigned seq,
    float distance,
    float speed,
    unsigned action_timeout_ms)
{
    static char payload[512];
    if (action_timeout_ms == 0U) {
        (void)snprintf(payload, sizeof(payload),
                       "{\"version\":1,\"task_id\":\"%s\",\"seq\":%u,"
                       "\"tool\":\"move_robot\",\"params\":{\"distance_m\":%.3f,"
                       "\"speed_mps\":%.3f},\"deadline_ms\":10000,\"sent_at_ms\":1000}",
                       task_id, seq, distance, speed);
    } else {
        (void)snprintf(payload, sizeof(payload),
                       "{\"version\":1,\"task_id\":\"%s\",\"seq\":%u,"
                       "\"tool\":\"move_robot\",\"params\":{\"distance_m\":%.3f,"
                       "\"speed_mps\":%.3f,\"timeout_ms\":%u},"
                       "\"deadline_ms\":10000,\"sent_at_ms\":1000}",
                       task_id, seq, distance, speed, action_timeout_ms);
    }
    return payload;
}

static robot_observation_t execute_json(
    robot_protocol_t *protocol,
    const char *payload,
    uint64_t prepare_ms,
    uint64_t execute_ms,
    robot_status_t expected)
{
    robot_ingest_result_t ingest;
    robot_observation_t observation;
    assert(robot_protocol_prepare_json(protocol, payload, prepare_ms, &ingest) == ROBOT_STATUS_SUCCESS);
    assert(ingest.disposition == ROBOT_INGEST_ENQUEUE);
    assert(robot_protocol_commit_pending(protocol, &ingest.command) == ROBOT_STATUS_PENDING);
    assert(robot_protocol_execute_command(
               protocol, &ingest.command, execute_ms, &observation) == expected);
    return observation;
}

int main(void)
{
    robot_protocol_t protocol;
    robot_protocol_t legacy;
    robot_command_t command;
    robot_command_t executed;
    robot_ingest_result_t ingest;
    robot_observation_t observation;
    robot_observation_t blocked;
    robot_ring_buffer_t ring;
    uint8_t byte;
    const char *payload;
    char json[ROBOT_OBSERVATION_JSON_CAPACITY];
    size_t written;
    cJSON *root;

    robot_ring_init(&ring);
    assert(robot_ring_push(&ring, 'A') == 1);
    assert(robot_ring_push(&ring, 'B') == 1);
    assert(robot_ring_pop(&ring, &byte) == 1 && byte == 'A');
    assert(robot_ring_pop(&ring, &byte) == 1 && byte == 'B');

    robot_protocol_init(&protocol);
    payload = move_json("c-test", 1U, 1.0f, 0.2f, 2500U);
    assert(robot_protocol_prepare_json(&protocol, payload, 1001U, &ingest) == ROBOT_STATUS_SUCCESS);
    assert(ingest.disposition == ROBOT_INGEST_ENQUEUE);
    assert(ingest.command.action_timeout_ms == 2500U);
    command = ingest.command;
    assert(robot_protocol_commit_pending(&protocol, &ingest.command) == ROBOT_STATUS_PENDING);
    assert(robot_protocol_prepare_json(&protocol, payload, 1001U, &ingest) == ROBOT_STATUS_PENDING);
    assert(ingest.disposition == ROBOT_INGEST_PENDING);
    assert(robot_protocol_execute_command(
               &protocol, &command, 1002U, &blocked) == ROBOT_STATUS_BLOCKED);
    assert(blocked.received_at_ms == 1002U);
    assert(strcmp(blocked.error_code, "front_obstacle") == 0);
    assert(protocol.state.executed_command_count == 0U);

    assert(robot_protocol_prepare_json(&protocol, payload, 1003U, &ingest) == ROBOT_STATUS_BLOCKED);
    assert(ingest.disposition == ROBOT_INGEST_REPLAY);
    assert(ingest.observation.received_at_ms == blocked.received_at_ms);
    assert(strcmp(ingest.observation.error_code, blocked.error_code) == 0);
    assert(protocol.state.executed_command_count == 0U);
    assert(robot_protocol_prepare_json(
               &protocol,
               move_json("c-test", 1U, 1.0f, 0.3f, 2500U),
               1003U,
               &ingest) == ROBOT_STATUS_REJECTED);
    assert(ingest.disposition == ROBOT_INGEST_REJECT);
    assert(strcmp(ingest.observation.error_code, "duplicate_conflict") == 0);

    assert(robot_protocol_set_obstacles(&protocol, 100.0f, 120.0f, 35.0f) == 1);
    observation = execute_json(
        &protocol,
        "{\"version\":1,\"task_id\":\"c-test\",\"seq\":2,"
        "\"tool\":\"turn_robot\",\"params\":{\"angle_deg\":90,"
        "\"angular_speed_dps\":45,\"timeout_ms\":2000},"
        "\"deadline_ms\":10000,\"sent_at_ms\":1000}",
        1003U,
        1004U,
        ROBOT_STATUS_SUCCESS);
    assert(observation.state.yaw_deg == 90.0f);
    payload = move_json("c-test", 3U, 1.0f, 0.2f, 0U);
    observation = execute_json(&protocol, payload, 1005U, 1006U, ROBOT_STATUS_SUCCESS);
    assert(observation.state.y_m > 0.99f && observation.state.y_m < 1.01f);
    assert(protocol.state.executed_command_count == 2U);
    assert(robot_protocol_prepare_json(&protocol, payload, 1007U, &ingest) == ROBOT_STATUS_SUCCESS);
    assert(ingest.disposition == ROBOT_INGEST_REPLAY);
    assert(protocol.state.executed_command_count == 2U);
    assert(protocol.state.y_m > 0.99f && protocol.state.y_m < 1.01f);

    observation = execute_json(
        &protocol,
        "{\"version\":1,\"task_id\":\"stale\",\"seq\":2,"
        "\"tool\":\"get_robot_state\",\"params\":{},"
        "\"deadline_ms\":10000,\"sent_at_ms\":1000}",
        1001U,
        1002U,
        ROBOT_STATUS_SUCCESS);
    (void)observation;
    assert(robot_protocol_prepare_json(
               &protocol,
               "{\"version\":1,\"task_id\":\"stale\",\"seq\":1,"
               "\"tool\":\"get_robot_state\",\"params\":{},"
               "\"deadline_ms\":10000,\"sent_at_ms\":1000}",
               1003U,
               &ingest) == ROBOT_STATUS_STALE);
    assert(strcmp(ingest.observation.error_code, "stale_sequence") == 0);

    assert(robot_protocol_prepare_json(
               &protocol,
               "{\"version\":1,\"task_id\":\"expired\",\"seq\":1,"
               "\"tool\":\"get_robot_state\",\"params\":{},"
               "\"deadline_ms\":100,\"sent_at_ms\":1000}",
               1100U,
               &ingest) == ROBOT_STATUS_TIMEOUT);
    assert(ingest.observation.received_at_ms == 1100U);
    assert(robot_protocol_prepare_json(
               &protocol,
               "{\"version\":1,\"task_id\":\"expired\",\"seq\":1,"
               "\"tool\":\"get_robot_state\",\"params\":{},"
               "\"deadline_ms\":100,\"sent_at_ms\":1000}",
               1200U,
               &ingest) == ROBOT_STATUS_TIMEOUT);
    assert(ingest.disposition == ROBOT_INGEST_REPLAY);
    assert(ingest.observation.received_at_ms == 1100U);

    assert(robot_protocol_prepare_json(
               &protocol,
               "{\"version\":1,\"task_id\":\"danger\",\"seq\":1,"
               "\"tool\":\"move_robot\",\"params\":{\"distance_m\":10,"
               "\"speed_mps\":5},\"deadline_ms\":10000,\"sent_at_ms\":1000}",
               1001U,
               &ingest) == ROBOT_STATUS_REJECTED);
    assert(robot_protocol_prepare_json(
               &protocol,
               "{\"version\":1,\"task_id\":\"stop-blank\",\"seq\":1,"
               "\"tool\":\"emergency_stop\",\"params\":{\"reason\":\"   \"},"
               "\"deadline_ms\":10000,\"sent_at_ms\":1000}",
               1001U,
               &ingest) == ROBOT_STATUS_REJECTED);

    observation = execute_json(
        &protocol,
        "{\"version\":1,\"task_id\":\"stop\",\"seq\":1,"
        "\"tool\":\"emergency_stop\",\"params\":{\"reason\":\" tilt \"},"
        "\"deadline_ms\":10000,\"sent_at_ms\":1000}",
        1001U,
        1002U,
        ROBOT_STATUS_EMERGENCY_STOP);
    assert(observation.state.emergency_stopped == 1U);
    payload = move_json("stop", 2U, 1.0f, 0.2f, 0U);
    observation = execute_json(&protocol, payload, 1003U, 1004U, ROBOT_STATUS_REJECTED);
    assert(strcmp(observation.error_code, "emergency_stopped") == 0);

    robot_protocol_init(&legacy);
    assert(robot_protocol_set_obstacles(&legacy, 100.0f, 120.0f, 35.0f) == 1);
    payload = move_json("legacy", 1U, 1.0f, 0.2f, 0U);
    assert(robot_protocol_ingest_json(&legacy, payload, 1001U, &command) == ROBOT_STATUS_SUCCESS);
    assert(robot_protocol_ingest_json(&legacy, payload, 1001U, &command) == ROBOT_STATUS_PENDING);
    assert(robot_protocol_dispatch_one(&legacy, 1002U, &executed) == ROBOT_STATUS_SUCCESS);
    assert(robot_protocol_ingest_json(&legacy, payload, 1003U, &command) == ROBOT_STATUS_SUCCESS);
    assert(robot_queue_count(&legacy.queue) == 0U);

    assert(robot_observation_to_json(&blocked, json, sizeof(json), &written) == 1);
    assert(written > 0U);
    root = cJSON_Parse(json);
    assert(root != NULL);
    assert(strcmp(cJSON_GetObjectItem(root, "task_id")->valuestring, "c-test") == 0);
    assert(strcmp(cJSON_GetObjectItem(root, "status")->valuestring, "blocked") == 0);
    assert(cJSON_GetObjectItem(root, "observation") != NULL);
    cJSON_Delete(root);
    (void)puts(json);
    return 0;
}
