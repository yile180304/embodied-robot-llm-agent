#ifndef EMBODIED_AGENT_ROBOT_PROTOCOL_INTERNAL_H
#define EMBODIED_AGENT_ROBOT_PROTOCOL_INTERNAL_H

#include "robot_protocol.h"

robot_status_t robot_protocol_parse_command_json(
    const char *payload,
    robot_command_t *command);

#endif
