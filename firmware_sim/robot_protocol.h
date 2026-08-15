#ifndef EMBODIED_AGENT_ROBOT_PROTOCOL_H
#define EMBODIED_AGENT_ROBOT_PROTOCOL_H

/*
 * Platform-neutral protocol and deterministic execution core.
 *
 * The public contract contains no HAL, socket, scheduler, or motor API.  A
 * runtime adapter may place Prepared Commands into a FreeRTOS Queue, while
 * host tests can use the fixed queue below with the same safety and
 * task_id+seq idempotency semantics.
 */

#include <stddef.h>
#include <stdint.h>

#define ROBOT_PROTOCOL_VERSION 1U
#define ROBOT_TASK_ID_MAX 64U
#define ROBOT_ERROR_CODE_MAX 64U
#define ROBOT_ERROR_MESSAGE_MAX 256U
#define ROBOT_OBSERVATION_JSON_CAPACITY 1024U
#define ROBOT_RING_CAPACITY 256U
#define ROBOT_COMMAND_QUEUE_CAPACITY 8U
#define ROBOT_IDEMPOTENCY_CACHE_CAPACITY 8U

typedef enum {
    ROBOT_TOOL_INVALID = 0,
    ROBOT_TOOL_MOVE = 1,
    ROBOT_TOOL_TURN = 2,
    ROBOT_TOOL_GET_STATE = 3,
    ROBOT_TOOL_SCAN = 4,
    ROBOT_TOOL_EMERGENCY_STOP = 5
} robot_tool_t;

typedef enum {
    ROBOT_STATUS_SUCCESS = 0,
    ROBOT_STATUS_BLOCKED = 1,
    ROBOT_STATUS_REJECTED = 2,
    ROBOT_STATUS_TIMEOUT = 3,
    ROBOT_STATUS_EMERGENCY_STOP = 4,
    ROBOT_STATUS_DUPLICATE = 5,
    ROBOT_STATUS_STALE = 6,
    ROBOT_STATUS_QUEUE_FULL = 7,
    ROBOT_STATUS_NO_COMMAND = 8,
    ROBOT_STATUS_PENDING = 9
} robot_status_t;

typedef struct {
    char task_id[ROBOT_TASK_ID_MAX + 1U];
    uint32_t seq;
    robot_tool_t tool;
    float distance_m;
    float speed_mps;
    float angle_deg;
    float angular_speed_dps;
    uint32_t action_timeout_ms;
    char reason[129];
    uint32_t deadline_ms;
    uint64_t sent_at_ms;
} robot_command_t;

typedef struct {
    float x_m;
    float y_m;
    float yaw_deg;
    float roll_deg;
    float pitch_deg;
    float front_distance_cm;
    float left_distance_cm;
    float right_distance_cm;
    uint8_t emergency_stopped;
    char last_task_id[ROBOT_TASK_ID_MAX + 1U];
    uint32_t last_seq;
    uint32_t executed_command_count;
} robot_state_t;

typedef struct {
    char task_id[ROBOT_TASK_ID_MAX + 1U];
    uint32_t seq;
    robot_status_t status;
    robot_state_t state;
    char error_code[ROBOT_ERROR_CODE_MAX + 1U];
    char error_message[ROBOT_ERROR_MESSAGE_MAX + 1U];
    uint64_t received_at_ms;
} robot_observation_t;

typedef struct {
    uint8_t bytes[ROBOT_RING_CAPACITY];
    size_t head;
    size_t tail;
    size_t count;
} robot_ring_buffer_t;

typedef struct {
    robot_command_t items[ROBOT_COMMAND_QUEUE_CAPACITY];
    size_t head;
    size_t tail;
    size_t count;
} robot_command_queue_t;

typedef struct {
    uint8_t used;
    uint8_t has_observation;
    char task_id[ROBOT_TASK_ID_MAX + 1U];
    uint32_t seq;
    uint32_t fingerprint;
    robot_status_t result;
    robot_observation_t observation;
} robot_idempotency_entry_t;

typedef enum {
    ROBOT_INGEST_ENQUEUE = 0,
    ROBOT_INGEST_REPLAY = 1,
    ROBOT_INGEST_REJECT = 2,
    ROBOT_INGEST_PENDING = 3
} robot_ingest_disposition_t;

typedef struct {
    robot_ingest_disposition_t disposition;
    robot_status_t status;
    robot_command_t command;
    robot_observation_t observation;
} robot_ingest_result_t;

typedef struct {
    robot_ring_buffer_t rx;
    robot_command_queue_t queue;
    robot_state_t state;
    robot_idempotency_entry_t cache[ROBOT_IDEMPOTENCY_CACHE_CAPACITY];
    size_t cache_next;
} robot_protocol_t;

void robot_ring_init(robot_ring_buffer_t *ring);
size_t robot_ring_count(const robot_ring_buffer_t *ring);
int robot_ring_push(robot_ring_buffer_t *ring, uint8_t byte);
int robot_ring_pop(robot_ring_buffer_t *ring, uint8_t *byte);

void robot_queue_init(robot_command_queue_t *queue);
size_t robot_queue_count(const robot_command_queue_t *queue);
int robot_queue_push(robot_command_queue_t *queue, const robot_command_t *command);
int robot_queue_pop(robot_command_queue_t *queue, robot_command_t *command);

void robot_protocol_init(robot_protocol_t *protocol);
int robot_protocol_set_obstacles(
    robot_protocol_t *protocol,
    float front_distance_cm,
    float left_distance_cm,
    float right_distance_cm);

robot_status_t robot_protocol_prepare_json(
    robot_protocol_t *protocol,
    const char *payload,
    uint64_t now_ms,
    robot_ingest_result_t *result);
robot_status_t robot_protocol_commit_pending(
    robot_protocol_t *protocol,
    const robot_command_t *command);
robot_status_t robot_protocol_execute_command(
    robot_protocol_t *protocol,
    const robot_command_t *command,
    uint64_t now_ms,
    robot_observation_t *observation);

/* Backward-compatible fixed-queue wrappers used by the protocol reference. */
robot_status_t robot_protocol_ingest_json(
    robot_protocol_t *protocol,
    const char *payload,
    uint64_t now_ms,
    robot_command_t *accepted_command);
robot_status_t robot_protocol_dispatch_one(
    robot_protocol_t *protocol,
    uint64_t now_ms,
    robot_command_t *executed_command);

const char *robot_status_name(robot_status_t status);
int robot_observation_to_json(
    const robot_observation_t *observation,
    char *buffer,
    size_t capacity,
    size_t *written);

#endif
