#include "robot_protocol_internal.h"

#include <math.h>
#include <string.h>

#include "cJSON.h"

static int finite_number(const cJSON *item, double *out)
{
    if (item == NULL || item->type != cJSON_Number || !isfinite(item->valuedouble)) {
        return 0;
    }
    *out = item->valuedouble;
    return 1;
}

static const cJSON *object_item(const cJSON *object, const char *name)
{
    if (object == NULL || object->type != cJSON_Object) {
        return NULL;
    }
    return cJSON_GetObjectItem((cJSON *)object, name);
}

static size_t key_match_count(const cJSON *object, const char *name)
{
    const cJSON *item;
    size_t matches = 0U;
    for (item = object->child; item != NULL; item = item->next) {
        if (item->string != NULL && strcmp(item->string, name) == 0) matches++;
    }
    return matches;
}

static int object_has_required_optional_keys(
    const cJSON *object,
    const char *const *required,
    size_t required_count,
    const char *const *optional,
    size_t optional_count)
{
    const cJSON *item;
    size_t i;
    if (object == NULL || object->type != cJSON_Object) return 0;
    for (item = object->child; item != NULL; item = item->next) {
        int found = 0;
        if (item->string == NULL) return 0;
        for (i = 0U; i < required_count; ++i) {
            if (strcmp(item->string, required[i]) == 0) {
                found = 1;
                break;
            }
        }
        if (!found) {
            for (i = 0U; i < optional_count; ++i) {
                if (strcmp(item->string, optional[i]) == 0) {
                    found = 1;
                    break;
                }
            }
        }
        if (!found) return 0;
    }
    for (i = 0U; i < required_count; ++i) {
        if (key_match_count(object, required[i]) != 1U) return 0;
    }
    for (i = 0U; i < optional_count; ++i) {
        if (key_match_count(object, optional[i]) > 1U) return 0;
    }
    return 1;
}

static int object_has_exact_keys(
    const cJSON *object,
    const char *const *required,
    size_t required_count)
{
    return object_has_required_optional_keys(object, required, required_count, NULL, 0U);
}

static int bounded_number(
    const cJSON *object,
    const char *name,
    double lower,
    double upper,
    double *out)
{
    const cJSON *item = object_item(object, name);
    double value = 0.0;
    if (!finite_number(item, &value) || value < lower || value > upper) {
        return 0;
    }
    *out = value;
    return 1;
}

static int copy_string(const cJSON *item, char *destination, size_t capacity)
{
    size_t length;
    if (item == NULL || item->type != cJSON_String || item->valuestring == NULL) {
        return 0;
    }
    length = strlen(item->valuestring);
    if (length == 0U || length >= capacity) {
        return 0;
    }
    memcpy(destination, item->valuestring, length + 1U);
    return 1;
}

static int copy_trimmed_string(const cJSON *item, char *destination, size_t capacity)
{
    const char *start;
    const char *end;
    size_t length;
    if (item == NULL || item->type != cJSON_String || item->valuestring == NULL) return 0;
    start = item->valuestring;
    while (*start == ' ' || *start == '\t' || *start == '\r' || *start == '\n') start++;
    end = start + strlen(start);
    while (end > start &&
           (end[-1] == ' ' || end[-1] == '\t' || end[-1] == '\r' || end[-1] == '\n')) {
        end--;
    }
    length = (size_t)(end - start);
    if (length == 0U || length >= capacity) return 0;
    memcpy(destination, start, length);
    destination[length] = '\0';
    return 1;
}

static int optional_bounded_integer(
    const cJSON *object,
    const char *name,
    double lower,
    double upper,
    uint32_t *out)
{
    const cJSON *item = object_item(object, name);
    double number;
    if (item == NULL) {
        *out = 0U;
        return 1;
    }
    if (!finite_number(item, &number) || number < lower || number > upper || floor(number) != number) {
        return 0;
    }
    *out = (uint32_t)number;
    return 1;
}

static int valid_task_id(const char *task_id)
{
    size_t i;
    size_t length = strlen(task_id);
    const char first = task_id[0];
    const int first_alpha_num = (first >= 'A' && first <= 'Z') ||
                                (first >= 'a' && first <= 'z') ||
                                (first >= '0' && first <= '9');
    if (length == 0U || length > ROBOT_TASK_ID_MAX) {
        return 0;
    }
    if (!first_alpha_num) return 0;
    for (i = 0U; i < length; ++i) {
        const char c = task_id[i];
        const int alpha_num = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                              (c >= '0' && c <= '9');
        if (!(alpha_num || c == '_' || c == '.' || c == ':' || c == '-')) {
            return 0;
        }
    }
    return 1;
}

static robot_tool_t parse_tool(const char *name)
{
    if (strcmp(name, "move_robot") == 0) return ROBOT_TOOL_MOVE;
    if (strcmp(name, "turn_robot") == 0) return ROBOT_TOOL_TURN;
    if (strcmp(name, "get_robot_state") == 0) return ROBOT_TOOL_GET_STATE;
    if (strcmp(name, "scan_obstacles") == 0) return ROBOT_TOOL_SCAN;
    if (strcmp(name, "emergency_stop") == 0) return ROBOT_TOOL_EMERGENCY_STOP;
    return ROBOT_TOOL_INVALID;
}

robot_status_t robot_protocol_parse_command_json(
    const char *payload,
    robot_command_t *command)
{
    cJSON *root = cJSON_Parse(payload);
    const cJSON *params;
    const cJSON *item;
    double number;
    char tool_name[32];
    static const char *const root_keys[] = {
        "version", "task_id", "seq", "tool", "params", "deadline_ms", "sent_at_ms"
    };
    static const char *const move_keys[] = {"distance_m", "speed_mps"};
    static const char *const turn_keys[] = {"angle_deg", "angular_speed_dps"};
    static const char *const action_optional_keys[] = {"timeout_ms"};
    static const char *const stop_keys[] = {"reason"};
    robot_status_t status = ROBOT_STATUS_REJECTED;

    if (root == NULL || root->type != cJSON_Object ||
        !object_has_exact_keys(root, root_keys, sizeof(root_keys) / sizeof(root_keys[0]))) goto cleanup;
    item = object_item(root, "version");
    if (!finite_number(item, &number) || number != (double)ROBOT_PROTOCOL_VERSION) goto cleanup;
    if (!copy_string(object_item(root, "task_id"), command->task_id, sizeof(command->task_id)) ||
        !valid_task_id(command->task_id)) goto cleanup;
    if (!bounded_number(root, "seq", 1.0, 4294967295.0, &number) || floor(number) != number) goto cleanup;
    command->seq = (uint32_t)number;
    if (!copy_string(object_item(root, "tool"), tool_name, sizeof(tool_name))) goto cleanup;
    command->tool = parse_tool(tool_name);
    if (command->tool == ROBOT_TOOL_INVALID) goto cleanup;
    params = object_item(root, "params");
    if (params == NULL || params->type != cJSON_Object) goto cleanup;
    if (!bounded_number(root, "deadline_ms", 1.0, 600000.0, &number) || floor(number) != number) goto cleanup;
    command->deadline_ms = (uint32_t)number;
    if (!bounded_number(root, "sent_at_ms", 0.0, 18446744073709551615.0, &number) || floor(number) != number) goto cleanup;
    command->sent_at_ms = (uint64_t)number;
    command->action_timeout_ms = 0U;
    memset(command->reason, 0, sizeof(command->reason));
    if (command->tool == ROBOT_TOOL_MOVE) {
        if (!object_has_required_optional_keys(
                params,
                move_keys,
                sizeof(move_keys) / sizeof(move_keys[0]),
                action_optional_keys,
                sizeof(action_optional_keys) / sizeof(action_optional_keys[0]))) goto cleanup;
        if (!bounded_number(params, "distance_m", -2.0, 2.0, &number)) goto cleanup;
        command->distance_m = (float)number;
        if (!bounded_number(params, "speed_mps", 0.05, 0.5, &number)) goto cleanup;
        command->speed_mps = (float)number;
        if (!optional_bounded_integer(params, "timeout_ms", 100.0, 10000.0,
                                      &command->action_timeout_ms)) goto cleanup;
    } else if (command->tool == ROBOT_TOOL_TURN) {
        if (!object_has_required_optional_keys(
                params,
                turn_keys,
                sizeof(turn_keys) / sizeof(turn_keys[0]),
                action_optional_keys,
                sizeof(action_optional_keys) / sizeof(action_optional_keys[0]))) goto cleanup;
        if (!bounded_number(params, "angle_deg", -180.0, 180.0, &number)) goto cleanup;
        command->angle_deg = (float)number;
        if (!bounded_number(params, "angular_speed_dps", 5.0, 180.0, &number)) goto cleanup;
        command->angular_speed_dps = (float)number;
        if (!optional_bounded_integer(params, "timeout_ms", 100.0, 10000.0,
                                      &command->action_timeout_ms)) goto cleanup;
    } else if (command->tool == ROBOT_TOOL_EMERGENCY_STOP) {
        if (!object_has_exact_keys(params, stop_keys, sizeof(stop_keys) / sizeof(stop_keys[0]))) goto cleanup;
        if (!copy_trimmed_string(object_item(params, "reason"), command->reason,
                                 sizeof(command->reason))) goto cleanup;
    } else if (command->tool == ROBOT_TOOL_GET_STATE || command->tool == ROBOT_TOOL_SCAN) {
        if (params->child != NULL) goto cleanup;
    }
    status = ROBOT_STATUS_SUCCESS;

cleanup:
    if (root != NULL) cJSON_Delete(root);
    return status;
}
