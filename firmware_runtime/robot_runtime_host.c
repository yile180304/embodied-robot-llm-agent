#include "robot_runtime_host.h"

#include <string.h>

static int host_queue_send(void *context, const robot_command_t *command)
{
    robot_runtime_host_t *host = (robot_runtime_host_t *)context;
    if (host->queue_count >= host->queue_capacity) return 0;
    host->queue[host->queue_head] = *command;
    host->queue_head = (host->queue_head + 1U) % ROBOT_HOST_QUEUE_CAPACITY;
    host->queue_count++;
    return 1;
}

static int host_queue_receive(void *context, robot_command_t *command)
{
    robot_runtime_host_t *host = (robot_runtime_host_t *)context;
    if (host->queue_count == 0U) return 0;
    *command = host->queue[host->queue_tail];
    host->queue_tail = (host->queue_tail + 1U) % ROBOT_HOST_QUEUE_CAPACITY;
    host->queue_count--;
    return 1;
}

static uint64_t host_clock_ms(void *context)
{
    return ((robot_runtime_host_t *)context)->now_ms;
}

static void host_observation_sink(
    void *context,
    const robot_observation_t *observation,
    const char *json,
    size_t json_length)
{
    robot_runtime_host_t *host = (robot_runtime_host_t *)context;
    const size_t index = host->observation_count;
    size_t copy_length;
    if (index >= ROBOT_HOST_OBSERVATION_CAPACITY) return;
    host->observations[index] = *observation;
    copy_length = json_length;
    if (copy_length >= ROBOT_OBSERVATION_JSON_CAPACITY) {
        copy_length = ROBOT_OBSERVATION_JSON_CAPACITY - 1U;
    }
    memcpy(host->observation_json[index], json, copy_length);
    host->observation_json[index][copy_length] = '\0';
    host->observation_count++;
}

int robot_runtime_host_init(robot_runtime_host_t *host, size_t queue_capacity)
{
    robot_runtime_port_t port;
    if (host == NULL || queue_capacity == 0U || queue_capacity > ROBOT_HOST_QUEUE_CAPACITY) return 0;
    memset(host, 0, sizeof(*host));
    host->queue_capacity = queue_capacity;
    port.queue_send = host_queue_send;
    port.queue_receive = host_queue_receive;
    port.clock_ms = host_clock_ms;
    port.observation_sink = host_observation_sink;
    port.context = host;
    return robot_runtime_init(&host->runtime, &port);
}

void robot_runtime_host_set_time(robot_runtime_host_t *host, uint64_t now_ms)
{
    if (host != NULL) host->now_ms = now_ms;
}

size_t robot_runtime_host_feed(robot_runtime_host_t *host, const char *bytes)
{
    if (host == NULL || bytes == NULL) return 0U;
    return robot_runtime_uart_rx_isr(
        &host->runtime,
        (const uint8_t *)bytes,
        strlen(bytes));
}

size_t robot_runtime_host_drain_parser(robot_runtime_host_t *host)
{
    size_t count = 0U;
    if (host == NULL) return 0U;
    while (robot_runtime_parser_step(&host->runtime)) count++;
    return count;
}

size_t robot_runtime_host_drain_control(robot_runtime_host_t *host)
{
    size_t count = 0U;
    if (host == NULL) return 0U;
    while (robot_runtime_control_step(&host->runtime)) count++;
    return count;
}

size_t robot_runtime_host_queue_count(const robot_runtime_host_t *host)
{
    return host == NULL ? 0U : host->queue_count;
}
