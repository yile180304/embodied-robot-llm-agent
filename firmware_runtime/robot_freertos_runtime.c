#include "robot_freertos_runtime.h"

#include <string.h>

static int freertos_queue_send(void *context, const robot_command_t *command)
{
    robot_freertos_runtime_t *binding = (robot_freertos_runtime_t *)context;
    return xQueueSend(binding->command_queue, command, 0U) == pdPASS;
}

static int freertos_queue_receive(void *context, robot_command_t *command)
{
    robot_freertos_runtime_t *binding = (robot_freertos_runtime_t *)context;
    return xQueueReceive(binding->command_queue, command, portMAX_DELAY) == pdPASS;
}

static uint64_t freertos_clock_ms(void *context)
{
    robot_freertos_runtime_t *binding = (robot_freertos_runtime_t *)context;
    return binding->hooks.clock_ms(binding->hooks.context);
}

static void freertos_observation_sink(
    void *context,
    const robot_observation_t *observation,
    const char *json,
    size_t json_length)
{
    robot_freertos_runtime_t *binding = (robot_freertos_runtime_t *)context;
    binding->hooks.observation_sink(
        binding->hooks.context,
        observation,
        json,
        json_length);
}

static void parser_task_main(void *context)
{
    robot_freertos_runtime_t *binding = (robot_freertos_runtime_t *)context;
    for (;;) {
        (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        while (robot_runtime_parser_step(&binding->runtime)) {
        }
    }
}

static void control_task_main(void *context)
{
    robot_freertos_runtime_t *binding = (robot_freertos_runtime_t *)context;
    for (;;) {
        (void)robot_runtime_control_step(&binding->runtime);
    }
}

int robot_freertos_runtime_init(
    robot_freertos_runtime_t *binding,
    const robot_freertos_runtime_hooks_t *hooks)
{
    robot_runtime_port_t runtime_port;
    if (binding == NULL || hooks == NULL || hooks->clock_ms == NULL ||
        hooks->observation_sink == NULL) return 0;

    memset(binding, 0, sizeof(*binding));
    binding->hooks = *hooks;
    binding->command_queue = xQueueCreateStatic(
        (UBaseType_t)ROBOT_FREERTOS_QUEUE_LENGTH,
        (UBaseType_t)sizeof(robot_command_t),
        binding->command_queue_storage,
        &binding->command_queue_control_block);
    if (binding->command_queue == NULL) return 0;

    runtime_port.queue_send = freertos_queue_send;
    runtime_port.queue_receive = freertos_queue_receive;
    runtime_port.clock_ms = freertos_clock_ms;
    runtime_port.observation_sink = freertos_observation_sink;
    runtime_port.context = binding;
    if (!robot_runtime_init(&binding->runtime, &runtime_port)) return 0;

    binding->parser_task = xTaskCreateStatic(
        parser_task_main,
        "robot-parser",
        (configSTACK_DEPTH_TYPE)ROBOT_FREERTOS_PARSER_STACK_DEPTH,
        binding,
        tskIDLE_PRIORITY + 2U,
        binding->parser_task_stack,
        &binding->parser_task_control_block);
    binding->control_task = xTaskCreateStatic(
        control_task_main,
        "robot-control",
        (configSTACK_DEPTH_TYPE)ROBOT_FREERTOS_CONTROL_STACK_DEPTH,
        binding,
        tskIDLE_PRIORITY + 1U,
        binding->control_task_stack,
        &binding->control_task_control_block);
    return binding->parser_task != NULL && binding->control_task != NULL;
}

size_t robot_freertos_runtime_uart_rx_isr(
    robot_freertos_runtime_t *binding,
    const uint8_t *bytes,
    size_t length)
{
    BaseType_t higher_priority_task_woken = pdFALSE;
    size_t accepted;
    if (binding == NULL || bytes == NULL) return 0U;
    accepted = robot_runtime_uart_rx_isr(&binding->runtime, bytes, length);
    if (length == 0U || binding->parser_task == NULL) return accepted;
    vTaskNotifyGiveFromISR(binding->parser_task, &higher_priority_task_woken);
    portYIELD_FROM_ISR(higher_priority_task_woken);
    return accepted;
}
