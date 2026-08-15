#ifndef EMBODIED_AGENT_FREERTOS_CONFIG_H
#define EMBODIED_AGENT_FREERTOS_CONFIG_H

/*
 * Generic Cortex-M4 compile configuration for the runtime adapter library.
 * Board integration must replace the clock and interrupt priority values with
 * values validated for the selected MCU before starting a scheduler.
 */
#define configCPU_CLOCK_HZ                         16000000UL
#define configTICK_RATE_HZ                         1000U
#define configUSE_PREEMPTION                       1
#define configUSE_TIME_SLICING                     1
#define configUSE_PORT_OPTIMISED_TASK_SELECTION    1
#define configUSE_TICKLESS_IDLE                    0
#define configMAX_PRIORITIES                       5
#define configMINIMAL_STACK_SIZE                   128U
#define configMAX_TASK_NAME_LEN                    16
#define configTICK_TYPE_WIDTH_IN_BITS              TICK_TYPE_WIDTH_32_BITS
#define configIDLE_SHOULD_YIELD                    1
#define configTASK_NOTIFICATION_ARRAY_ENTRIES      1
#define configQUEUE_REGISTRY_SIZE                  0
#define configENABLE_BACKWARD_COMPATIBILITY        0
#define configNUM_THREAD_LOCAL_STORAGE_POINTERS    0
#define configUSE_MINI_LIST_ITEM                   1
#define configUSE_NEWLIB_REENTRANT                 0

#define configUSE_TIMERS                           0
#define configSUPPORT_STATIC_ALLOCATION            1
#define configSUPPORT_DYNAMIC_ALLOCATION           0
#define configKERNEL_PROVIDED_STATIC_MEMORY        1

#define configPRIO_BITS                            4U
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY    15U
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY 5U
#define configKERNEL_INTERRUPT_PRIORITY            \
    (configLIBRARY_LOWEST_INTERRUPT_PRIORITY << (8U - configPRIO_BITS))
#define configMAX_SYSCALL_INTERRUPT_PRIORITY       \
    (configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY << (8U - configPRIO_BITS))
#define configMAX_API_CALL_INTERRUPT_PRIORITY      configMAX_SYSCALL_INTERRUPT_PRIORITY
#define configCHECK_HANDLER_INSTALLATION           0

#define configUSE_IDLE_HOOK                        0
#define configUSE_TICK_HOOK                        0
#define configUSE_MALLOC_FAILED_HOOK               0
#define configCHECK_FOR_STACK_OVERFLOW             0
#define configGENERATE_RUN_TIME_STATS              0
#define configUSE_TRACE_FACILITY                   0
#define configUSE_STATS_FORMATTING_FUNCTIONS       0

#define configUSE_CO_ROUTINES                      0
#define configMAX_CO_ROUTINE_PRIORITIES            1
#define configUSE_TASK_NOTIFICATIONS               1
#define configUSE_MUTEXES                          0
#define configUSE_RECURSIVE_MUTEXES                0
#define configUSE_COUNTING_SEMAPHORES              0
#define configUSE_QUEUE_SETS                       0
#define configUSE_APPLICATION_TASK_TAG             0
#define configUSE_POSIX_ERRNO                      0

#define INCLUDE_vTaskPrioritySet                   0
#define INCLUDE_uxTaskPriorityGet                  0
#define INCLUDE_vTaskDelete                        0
#define INCLUDE_vTaskSuspend                       1
#define INCLUDE_vTaskDelayUntil                    0
#define INCLUDE_vTaskDelay                         0
#define INCLUDE_xTaskGetSchedulerState             0
#define INCLUDE_xTaskGetCurrentTaskHandle          0
#define INCLUDE_uxTaskGetStackHighWaterMark        0
#define INCLUDE_xTaskGetIdleTaskHandle             0
#define INCLUDE_eTaskGetState                      0
#define INCLUDE_xTimerPendFunctionCall             0
#define INCLUDE_xTaskAbortDelay                    0
#define INCLUDE_xTaskGetHandle                     0
#define INCLUDE_xTaskResumeFromISR                 0

#endif
