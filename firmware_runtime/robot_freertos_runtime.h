#ifndef EMBODIED_AGENT_ROBOT_FREERTOS_RUNTIME_H
#define EMBODIED_AGENT_ROBOT_FREERTOS_RUNTIME_H

#include <stddef.h>
#include <stdint.h>

#include "FreeRTOS.h"
#include "queue.h"
#include "task.h"

#include "robot_runtime.h"

#define ROBOT_FREERTOS_QUEUE_LENGTH 8U
#define ROBOT_FREERTOS_PARSER_STACK_DEPTH (configMINIMAL_STACK_SIZE * 4U)
#define ROBOT_FREERTOS_CONTROL_STACK_DEPTH (configMINIMAL_STACK_SIZE * 4U)

typedef struct {
    robot_runtime_clock_fn clock_ms;
    robot_runtime_observation_sink_fn observation_sink;
    void *context;
} robot_freertos_runtime_hooks_t;

typedef struct {
    robot_runtime_t runtime;
    robot_freertos_runtime_hooks_t hooks;
    StaticQueue_t command_queue_control_block;
    uint8_t command_queue_storage[ROBOT_FREERTOS_QUEUE_LENGTH * sizeof(robot_command_t)];
    QueueHandle_t command_queue;
    StaticTask_t parser_task_control_block;
    StackType_t parser_task_stack[ROBOT_FREERTOS_PARSER_STACK_DEPTH];
    TaskHandle_t parser_task;
    StaticTask_t control_task_control_block;
    StackType_t control_task_stack[ROBOT_FREERTOS_CONTROL_STACK_DEPTH];
    TaskHandle_t control_task;
} robot_freertos_runtime_t;

int robot_freertos_runtime_init(
    robot_freertos_runtime_t *binding,
    const robot_freertos_runtime_hooks_t *hooks);

/* Board ISR adapters may call this with one UART/DMA chunk. */
size_t robot_freertos_runtime_uart_rx_isr(
    robot_freertos_runtime_t *binding,
    const uint8_t *bytes,
    size_t length);

#endif
