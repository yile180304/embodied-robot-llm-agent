#include "robot_protocol.h"

#include "robot_protocol_internal.h"

#include <math.h>
#include <string.h>

#include "cJSON.h"

static uint32_t hash_bytes(uint32_t hash, const unsigned char *bytes, size_t length)
{
    size_t i;
    for (i = 0U; i < length; ++i) {
        hash ^= bytes[i];
        hash *= 16777619U;
    }
    return hash;
}

static uint32_t command_fingerprint(const robot_command_t *command)
{
    uint32_t hash = 2166136261U;
    hash = hash_bytes(hash, (const unsigned char *)command->task_id, strlen(command->task_id));
    hash = hash_bytes(hash, (const unsigned char *)&command->seq, sizeof(command->seq));
    hash = hash_bytes(hash, (const unsigned char *)&command->tool, sizeof(command->tool));
    hash = hash_bytes(hash, (const unsigned char *)&command->distance_m, sizeof(command->distance_m));
    hash = hash_bytes(hash, (const unsigned char *)&command->speed_mps, sizeof(command->speed_mps));
    hash = hash_bytes(hash, (const unsigned char *)&command->angle_deg, sizeof(command->angle_deg));
    hash = hash_bytes(hash, (const unsigned char *)&command->angular_speed_dps, sizeof(command->angular_speed_dps));
    hash = hash_bytes(hash, (const unsigned char *)&command->action_timeout_ms, sizeof(command->action_timeout_ms));
    hash = hash_bytes(hash, (const unsigned char *)&command->deadline_ms, sizeof(command->deadline_ms));
    hash = hash_bytes(hash, (const unsigned char *)&command->sent_at_ms, sizeof(command->sent_at_ms));
    hash = hash_bytes(hash, (const unsigned char *)command->reason, strlen(command->reason));
    return hash;
}

static robot_idempotency_entry_t *find_cache(
    robot_protocol_t *protocol,
    const char *task_id,
    uint32_t seq)
{
    size_t i;
    for (i = 0U; i < ROBOT_IDEMPOTENCY_CACHE_CAPACITY; ++i) {
        robot_idempotency_entry_t *entry = &protocol->cache[i];
        if (entry->used && entry->seq == seq && strcmp(entry->task_id, task_id) == 0) {
            return entry;
        }
    }
    return NULL;
}

static robot_idempotency_entry_t *allocate_cache_entry(
    robot_protocol_t *protocol,
    const robot_command_t *command)
{
    robot_idempotency_entry_t *entry = &protocol->cache[protocol->cache_next];
    memset(entry, 0, sizeof(*entry));
    entry->used = 1U;
    (void)strncpy(entry->task_id, command->task_id, ROBOT_TASK_ID_MAX);
    entry->task_id[ROBOT_TASK_ID_MAX] = '\0';
    entry->seq = command->seq;
    entry->fingerprint = command_fingerprint(command);
    protocol->cache_next = (protocol->cache_next + 1U) % ROBOT_IDEMPOTENCY_CACHE_CAPACITY;
    return entry;
}

static void remember_pending(robot_protocol_t *protocol, const robot_command_t *command)
{
    robot_idempotency_entry_t *entry = allocate_cache_entry(protocol, command);
    entry->result = ROBOT_STATUS_PENDING;
}

static void remember_observation(
    robot_protocol_t *protocol,
    const robot_command_t *command,
    const robot_observation_t *observation)
{
    robot_idempotency_entry_t *entry = find_cache(protocol, command->task_id, command->seq);
    if (entry == NULL) entry = allocate_cache_entry(protocol, command);
    entry->result = observation->status;
    entry->has_observation = 1U;
    entry->observation = *observation;
}

static uint32_t latest_seq_for_task(const robot_protocol_t *protocol, const char *task_id)
{
    size_t i;
    uint32_t latest = 0U;
    for (i = 0U; i < ROBOT_IDEMPOTENCY_CACHE_CAPACITY; ++i) {
        const robot_idempotency_entry_t *entry = &protocol->cache[i];
        if (entry->used && strcmp(entry->task_id, task_id) == 0 && entry->seq > latest) {
            latest = entry->seq;
        }
    }
    return latest;
}

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

static void make_observation(
    const robot_protocol_t *protocol,
    const robot_command_t *command,
    robot_status_t status,
    uint64_t now_ms,
    const char *error_code,
    const char *error_message,
    robot_observation_t *observation)
{
    memset(observation, 0, sizeof(*observation));
    copy_text(observation->task_id, sizeof(observation->task_id), command->task_id);
    observation->seq = command->seq;
    observation->status = status;
    observation->state = protocol->state;
    copy_text(observation->error_code, sizeof(observation->error_code), error_code);
    copy_text(observation->error_message, sizeof(observation->error_message), error_message);
    observation->received_at_ms = now_ms;
}

static void make_invalid_observation(
    const robot_protocol_t *protocol,
    uint64_t now_ms,
    const char *error_code,
    const char *error_message,
    robot_observation_t *observation)
{
    robot_command_t command;
    memset(&command, 0, sizeof(command));
    copy_text(command.task_id, sizeof(command.task_id), "invalid-command");
    command.seq = 1U;
    make_observation(
        protocol,
        &command,
        ROBOT_STATUS_REJECTED,
        now_ms,
        error_code,
        error_message,
        observation);
}

void robot_ring_init(robot_ring_buffer_t *ring)
{
    if (ring != NULL) memset(ring, 0, sizeof(*ring));
}

size_t robot_ring_count(const robot_ring_buffer_t *ring)
{
    return ring == NULL ? 0U : ring->count;
}

int robot_ring_push(robot_ring_buffer_t *ring, uint8_t byte)
{
    if (ring == NULL || ring->count >= ROBOT_RING_CAPACITY) return 0;
    ring->bytes[ring->head] = byte;
    ring->head = (ring->head + 1U) % ROBOT_RING_CAPACITY;
    ring->count++;
    return 1;
}

int robot_ring_pop(robot_ring_buffer_t *ring, uint8_t *byte)
{
    if (ring == NULL || byte == NULL || ring->count == 0U) return 0;
    *byte = ring->bytes[ring->tail];
    ring->tail = (ring->tail + 1U) % ROBOT_RING_CAPACITY;
    ring->count--;
    return 1;
}

void robot_queue_init(robot_command_queue_t *queue)
{
    if (queue != NULL) memset(queue, 0, sizeof(*queue));
}

size_t robot_queue_count(const robot_command_queue_t *queue)
{
    return queue == NULL ? 0U : queue->count;
}

int robot_queue_push(robot_command_queue_t *queue, const robot_command_t *command)
{
    if (queue == NULL || command == NULL || queue->count >= ROBOT_COMMAND_QUEUE_CAPACITY) return 0;
    queue->items[queue->head] = *command;
    queue->head = (queue->head + 1U) % ROBOT_COMMAND_QUEUE_CAPACITY;
    queue->count++;
    return 1;
}

int robot_queue_pop(robot_command_queue_t *queue, robot_command_t *command)
{
    if (queue == NULL || command == NULL || queue->count == 0U) return 0;
    *command = queue->items[queue->tail];
    queue->tail = (queue->tail + 1U) % ROBOT_COMMAND_QUEUE_CAPACITY;
    queue->count--;
    return 1;
}

void robot_protocol_init(robot_protocol_t *protocol)
{
    if (protocol == NULL) return;
    memset(protocol, 0, sizeof(*protocol));
    robot_ring_init(&protocol->rx);
    robot_queue_init(&protocol->queue);
    protocol->state.front_distance_cm = 18.0f;
    protocol->state.left_distance_cm = 120.0f;
    protocol->state.right_distance_cm = 35.0f;
}

int robot_protocol_set_obstacles(
    robot_protocol_t *protocol,
    float front_distance_cm,
    float left_distance_cm,
    float right_distance_cm)
{
    if (protocol == NULL || !isfinite(front_distance_cm) || !isfinite(left_distance_cm) ||
        !isfinite(right_distance_cm) || front_distance_cm < 0.0f ||
        left_distance_cm < 0.0f || right_distance_cm < 0.0f) return 0;
    protocol->state.front_distance_cm = front_distance_cm;
    protocol->state.left_distance_cm = left_distance_cm;
    protocol->state.right_distance_cm = right_distance_cm;
    return 1;
}

robot_status_t robot_protocol_prepare_json(
    robot_protocol_t *protocol,
    const char *payload,
    uint64_t now_ms,
    robot_ingest_result_t *result)
{
    robot_command_t command;
    robot_idempotency_entry_t *cached;
    uint32_t latest;
    if (protocol == NULL || payload == NULL || result == NULL) return ROBOT_STATUS_REJECTED;
    memset(result, 0, sizeof(*result));
    memset(&command, 0, sizeof(command));
    if (robot_protocol_parse_command_json(payload, &command) != ROBOT_STATUS_SUCCESS) {
        result->disposition = ROBOT_INGEST_REJECT;
        result->status = ROBOT_STATUS_REJECTED;
        make_invalid_observation(
            protocol,
            now_ms,
            "schema_validation_error",
            "command JSON failed strict schema or range validation",
            &result->observation);
        return result->status;
    }

    cached = find_cache(protocol, command.task_id, command.seq);
    if (cached != NULL) {
        if (cached->fingerprint != command_fingerprint(&command)) {
            result->disposition = ROBOT_INGEST_REJECT;
            result->status = ROBOT_STATUS_REJECTED;
            make_observation(
                protocol,
                &command,
                ROBOT_STATUS_REJECTED,
                now_ms,
                "duplicate_conflict",
                "same task_id and seq carried different command data",
                &result->observation);
            return result->status;
        }
        if (cached->has_observation) {
            result->disposition = ROBOT_INGEST_REPLAY;
            result->status = cached->result;
            result->observation = cached->observation;
            return result->status;
        }
        result->disposition = ROBOT_INGEST_PENDING;
        result->status = ROBOT_STATUS_PENDING;
        return result->status;
    }

    if (now_ms >= command.sent_at_ms + (uint64_t)command.deadline_ms) {
        result->disposition = ROBOT_INGEST_REJECT;
        result->status = ROBOT_STATUS_TIMEOUT;
        make_observation(
            protocol,
            &command,
            ROBOT_STATUS_TIMEOUT,
            now_ms,
            "deadline_expired",
            "command deadline expired before queueing",
            &result->observation);
        remember_observation(protocol, &command, &result->observation);
        return result->status;
    }

    latest = latest_seq_for_task(protocol, command.task_id);
    if (latest != 0U && command.seq < latest) {
        result->disposition = ROBOT_INGEST_REJECT;
        result->status = ROBOT_STATUS_STALE;
        make_observation(
            protocol,
            &command,
            ROBOT_STATUS_STALE,
            now_ms,
            "stale_sequence",
            "sequence is older than the latest accepted sequence",
            &result->observation);
        remember_observation(protocol, &command, &result->observation);
        return result->status;
    }

    result->disposition = ROBOT_INGEST_ENQUEUE;
    result->status = ROBOT_STATUS_SUCCESS;
    result->command = command;
    return result->status;
}

robot_status_t robot_protocol_commit_pending(
    robot_protocol_t *protocol,
    const robot_command_t *command)
{
    robot_idempotency_entry_t *cached;
    if (protocol == NULL || command == NULL) return ROBOT_STATUS_REJECTED;
    cached = find_cache(protocol, command->task_id, command->seq);
    if (cached != NULL) {
        if (cached->fingerprint != command_fingerprint(command)) return ROBOT_STATUS_REJECTED;
        return cached->has_observation ? cached->result : ROBOT_STATUS_PENDING;
    }
    remember_pending(protocol, command);
    return ROBOT_STATUS_PENDING;
}

robot_status_t robot_protocol_execute_command(
    robot_protocol_t *protocol,
    const robot_command_t *command,
    uint64_t now_ms,
    robot_observation_t *observation)
{
    robot_status_t status = ROBOT_STATUS_REJECTED;
    const char *error_code = NULL;
    const char *error_message = NULL;
    if (protocol == NULL || command == NULL || observation == NULL) return ROBOT_STATUS_REJECTED;

    copy_text(protocol->state.last_task_id, sizeof(protocol->state.last_task_id), command->task_id);
    protocol->state.last_seq = command->seq;
    if (now_ms >= command->sent_at_ms + (uint64_t)command->deadline_ms) {
        status = ROBOT_STATUS_TIMEOUT;
        error_code = "deadline_expired";
        error_message = "command deadline expired before execution";
    } else if (command->tool == ROBOT_TOOL_EMERGENCY_STOP) {
        protocol->state.emergency_stopped = 1U;
        protocol->state.executed_command_count++;
        status = ROBOT_STATUS_EMERGENCY_STOP;
    } else if (protocol->state.emergency_stopped &&
               (command->tool == ROBOT_TOOL_MOVE || command->tool == ROBOT_TOOL_TURN)) {
        status = ROBOT_STATUS_REJECTED;
        error_code = "emergency_stopped";
        error_message = "motion is disabled after emergency stop";
    } else if (command->tool == ROBOT_TOOL_MOVE && command->distance_m > 0.0f &&
               protocol->state.front_distance_cm < 25.0f) {
        status = ROBOT_STATUS_BLOCKED;
        error_code = "front_obstacle";
        error_message = "front obstacle is below the safety distance";
    } else if (command->tool == ROBOT_TOOL_MOVE) {
        const float radians = protocol->state.yaw_deg * 0.017453292519943295f;
        protocol->state.x_m += command->distance_m * cosf(radians);
        protocol->state.y_m += command->distance_m * sinf(radians);
        protocol->state.executed_command_count++;
        status = ROBOT_STATUS_SUCCESS;
    } else if (command->tool == ROBOT_TOOL_TURN) {
        protocol->state.yaw_deg += command->angle_deg;
        while (protocol->state.yaw_deg > 180.0f) protocol->state.yaw_deg -= 360.0f;
        while (protocol->state.yaw_deg <= -180.0f) protocol->state.yaw_deg += 360.0f;
        protocol->state.front_distance_cm = command->angle_deg >= 0.0f ?
            protocol->state.left_distance_cm : protocol->state.right_distance_cm;
        protocol->state.executed_command_count++;
        status = ROBOT_STATUS_SUCCESS;
    } else if (command->tool == ROBOT_TOOL_GET_STATE || command->tool == ROBOT_TOOL_SCAN) {
        protocol->state.executed_command_count++;
        status = ROBOT_STATUS_SUCCESS;
    } else {
        status = ROBOT_STATUS_REJECTED;
        error_code = "unregistered_tool";
        error_message = "tool is not in the device allowlist";
    }

    make_observation(protocol, command, status, now_ms, error_code, error_message, observation);
    remember_observation(protocol, command, observation);
    return status;
}

robot_status_t robot_protocol_ingest_json(
    robot_protocol_t *protocol,
    const char *payload,
    uint64_t now_ms,
    robot_command_t *accepted_command)
{
    robot_ingest_result_t result;
    robot_status_t status;
    if (accepted_command == NULL) return ROBOT_STATUS_REJECTED;
    status = robot_protocol_prepare_json(protocol, payload, now_ms, &result);
    if (result.disposition != ROBOT_INGEST_ENQUEUE) return status;
    if (!robot_queue_push(&protocol->queue, &result.command)) return ROBOT_STATUS_QUEUE_FULL;
    (void)robot_protocol_commit_pending(protocol, &result.command);
    *accepted_command = result.command;
    return ROBOT_STATUS_SUCCESS;
}

robot_status_t robot_protocol_dispatch_one(
    robot_protocol_t *protocol,
    uint64_t now_ms,
    robot_command_t *executed_command)
{
    robot_command_t command;
    robot_observation_t observation;
    if (protocol == NULL || executed_command == NULL || !robot_queue_pop(&protocol->queue, &command)) {
        return ROBOT_STATUS_NO_COMMAND;
    }
    *executed_command = command;
    return robot_protocol_execute_command(protocol, &command, now_ms, &observation);
}

const char *robot_status_name(robot_status_t status)
{
    switch (status) {
    case ROBOT_STATUS_SUCCESS: return "success";
    case ROBOT_STATUS_BLOCKED: return "blocked";
    case ROBOT_STATUS_TIMEOUT: return "timeout";
    case ROBOT_STATUS_EMERGENCY_STOP: return "emergency_stop";
    case ROBOT_STATUS_REJECTED:
    case ROBOT_STATUS_STALE:
    case ROBOT_STATUS_QUEUE_FULL:
    case ROBOT_STATUS_DUPLICATE:
    case ROBOT_STATUS_NO_COMMAND:
    case ROBOT_STATUS_PENDING:
    default:
        return "rejected";
    }
}

static int add_state_json(cJSON *object, const robot_state_t *state)
{
    if (cJSON_AddNumberToObject(object, "x_m", state->x_m) == NULL) return 0;
    if (cJSON_AddNumberToObject(object, "y_m", state->y_m) == NULL) return 0;
    if (cJSON_AddNumberToObject(object, "yaw_deg", state->yaw_deg) == NULL) return 0;
    if (cJSON_AddNumberToObject(object, "roll_deg", state->roll_deg) == NULL) return 0;
    if (cJSON_AddNumberToObject(object, "pitch_deg", state->pitch_deg) == NULL) return 0;
    if (cJSON_AddNumberToObject(object, "front_distance_cm", state->front_distance_cm) == NULL) return 0;
    if (cJSON_AddNumberToObject(object, "left_distance_cm", state->left_distance_cm) == NULL) return 0;
    if (cJSON_AddNumberToObject(object, "right_distance_cm", state->right_distance_cm) == NULL) return 0;
    if (cJSON_AddBoolToObject(object, "emergency_stopped", state->emergency_stopped != 0U) == NULL) return 0;
    if (state->last_task_id[0] != '\0') {
        if (cJSON_AddStringToObject(object, "last_task_id", state->last_task_id) == NULL) return 0;
        if (cJSON_AddNumberToObject(object, "last_seq", state->last_seq) == NULL) return 0;
    }
    return 1;
}

int robot_observation_to_json(
    const robot_observation_t *observation,
    char *buffer,
    size_t capacity,
    size_t *written)
{
    cJSON *root;
    cJSON *details;
    int ok = 0;
    if (observation == NULL || buffer == NULL || capacity == 0U || written == NULL) return 0;
    *written = 0U;
    root = cJSON_CreateObject();
    if (root == NULL) return 0;
    details = cJSON_CreateObject();
    if (details == NULL) goto cleanup;
    if (!cJSON_AddItemToObject(root, "observation", details)) {
        cJSON_Delete(details);
        goto cleanup;
    }
    if (cJSON_AddNumberToObject(root, "version", ROBOT_PROTOCOL_VERSION) == NULL) goto cleanup;
    if (cJSON_AddStringToObject(root, "task_id", observation->task_id) == NULL) goto cleanup;
    if (cJSON_AddNumberToObject(root, "seq", observation->seq) == NULL) goto cleanup;
    if (cJSON_AddStringToObject(root, "status", robot_status_name(observation->status)) == NULL) goto cleanup;
    if (!add_state_json(details, &observation->state)) goto cleanup;
    if (observation->error_code[0] == '\0') {
        if (cJSON_AddNullToObject(root, "error_code") == NULL) goto cleanup;
    } else if (cJSON_AddStringToObject(root, "error_code", observation->error_code) == NULL) {
        goto cleanup;
    }
    if (observation->error_message[0] == '\0') {
        if (cJSON_AddNullToObject(root, "error_message") == NULL) goto cleanup;
    } else if (cJSON_AddStringToObject(root, "error_message", observation->error_message) == NULL) {
        goto cleanup;
    }
    if (cJSON_AddNumberToObject(root, "received_at_ms", (double)observation->received_at_ms) == NULL) {
        goto cleanup;
    }
    if (!cJSON_PrintPreallocated(root, buffer, (int)capacity, 0)) goto cleanup;
    *written = strlen(buffer);
    ok = 1;

cleanup:
    cJSON_Delete(root);
    return ok;
}
