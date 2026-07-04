#include "wifi_app.h"

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "beep.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "freertos/timers.h"
#include "lvgl_app.h"

#define WIFI_TARGET_SSID       "Tenda_3D12A0"
#define WIFI_TARGET_PASSWORD   "15024423610"
#define WIFI_RETRY_DELAY_MS    5000
#define WIFI_TASK_STACK_SIZE   (4 * 1024)
#define WIFI_TASK_PRIORITY     4
#define WIFI_EVENT_QUEUE_SIZE  8
#define WIFI_BEEP_TIME_MS      1000

typedef enum
{
    WIFI_APP_EVENT_STARTED,
    WIFI_APP_EVENT_DISCONNECTED,
    WIFI_APP_EVENT_GOT_IP,
} wifi_app_event_t;

static const char *TAG = "wifi_app";
static QueueHandle_t wifi_event_queue;
static TimerHandle_t wifi_beep_timer;
static volatile bool wifi_connected;

static void wifi_beep_timer_callback(TimerHandle_t timer)
{
    (void)timer;
    BEEP_OFF();
}

static void wifi_beep_stop(void)
{
    BEEP_OFF();

    if (wifi_beep_timer != NULL)
    {
        (void)xTimerStop(wifi_beep_timer, 0);
    }
}

static void wifi_beep_once(void)
{
    if (wifi_beep_timer == NULL)
    {
        ESP_LOGW(TAG, "Wi-Fi beep timer is not ready");
        return;
    }

    BEEP_ON();
    if (xTimerReset(wifi_beep_timer, 0) != pdPASS)
    {
        BEEP_OFF();
        ESP_LOGW(TAG, "Unable to start Wi-Fi beep timer");
    }
}

static void wifi_set_icon(bool connected)
{
    esp_err_t error = lvgl_app_set_wifi_connected(connected);

    if (error != ESP_OK && error != ESP_ERR_TIMEOUT)
    {
        ESP_LOGW(TAG, "Unable to update Wi-Fi icon: %s",
                 esp_err_to_name(error));
    }
}

static void wifi_post_event(wifi_app_event_t event)
{
    if (wifi_event_queue == NULL ||
        xQueueSend(wifi_event_queue, &event, 0) != pdTRUE)
    {
        ESP_LOGW(TAG, "Wi-Fi event queue is full");
    }
}

static void wifi_event_handler(void *argument,
                               esp_event_base_t event_base,
                               int32_t event_id,
                               void *event_data)
{
    (void)argument;

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START)
    {
        wifi_post_event(WIFI_APP_EVENT_STARTED);
    }
    else if (event_base == WIFI_EVENT &&
             event_id == WIFI_EVENT_STA_DISCONNECTED)
    {
        const wifi_event_sta_disconnected_t *event =
            (const wifi_event_sta_disconnected_t *)event_data;

        wifi_connected = false;
        wifi_beep_stop();
        wifi_set_icon(false);
        ESP_LOGW(TAG, "Disconnected from %s, reason=%u",
                 WIFI_TARGET_SSID,
                 event != NULL ? event->reason : 0);
        wifi_post_event(WIFI_APP_EVENT_DISCONNECTED);
    }
    else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP)
    {
        const ip_event_got_ip_t *event =
            (const ip_event_got_ip_t *)event_data;

        if (!wifi_connected)
        {
            wifi_beep_once();
        }
        wifi_connected = true;
        wifi_set_icon(true);
        if (event != NULL)
        {
            ESP_LOGI(TAG, "Connected to %s, IP=" IPSTR,
                     WIFI_TARGET_SSID,
                     IP2STR(&event->ip_info.ip));
        }
        wifi_post_event(WIFI_APP_EVENT_GOT_IP);
    }
}

static esp_err_t wifi_scan_and_connect(void)
{
    uint8_t target_ssid[] = WIFI_TARGET_SSID;
    wifi_scan_config_t scan_config = {
        .ssid = target_ssid,
        .bssid = NULL,
        .channel = 0,
        .show_hidden = true,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
    };

    wifi_set_icon(false);
    ESP_LOGI(TAG, "Scanning for SSID: %s", WIFI_TARGET_SSID);

    esp_err_t error = esp_wifi_scan_start(&scan_config, true);
    if (error != ESP_OK)
    {
        ESP_LOGE(TAG, "Wi-Fi scan failed: %s", esp_err_to_name(error));
        return error;
    }

    uint16_t ap_count = 0;
    error = esp_wifi_scan_get_ap_num(&ap_count);
    if (error != ESP_OK)
    {
        esp_wifi_clear_ap_list();
        ESP_LOGE(TAG, "Unable to read scan result: %s",
                 esp_err_to_name(error));
        return error;
    }

    if (ap_count == 0)
    {
        esp_wifi_clear_ap_list();
        ESP_LOGW(TAG, "SSID %s was not found", WIFI_TARGET_SSID);
        return ESP_ERR_NOT_FOUND;
    }

    wifi_ap_record_t ap_record = {0};
    error = esp_wifi_scan_get_ap_record(&ap_record);
    esp_wifi_clear_ap_list();
    if (error != ESP_OK)
    {
        ESP_LOGE(TAG, "Unable to obtain AP information: %s",
                 esp_err_to_name(error));
        return error;
    }

    if (strcmp((const char *)ap_record.ssid, WIFI_TARGET_SSID) != 0)
    {
        ESP_LOGW(TAG, "Target SSID was not present in filtered results");
        return ESP_ERR_NOT_FOUND;
    }

    ESP_LOGI(TAG, "Found %s, RSSI=%d, channel=%u",
             WIFI_TARGET_SSID,
             ap_record.rssi,
             ap_record.primary);

    wifi_config_t station_config = {0};
    memcpy(station_config.sta.ssid,
           WIFI_TARGET_SSID,
           sizeof(WIFI_TARGET_SSID));
    memcpy(station_config.sta.password,
           WIFI_TARGET_PASSWORD,
           sizeof(WIFI_TARGET_PASSWORD));
    station_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    station_config.sta.pmf_cfg.capable = true;
    station_config.sta.pmf_cfg.required = false;

    error = esp_wifi_set_config(WIFI_IF_STA, &station_config);
    if (error != ESP_OK)
    {
        ESP_LOGE(TAG, "Unable to set station configuration: %s",
                 esp_err_to_name(error));
        return error;
    }

    ESP_LOGI(TAG, "Connecting to %s", WIFI_TARGET_SSID);
    error = esp_wifi_connect();
    if (error != ESP_OK)
    {
        ESP_LOGE(TAG, "Wi-Fi connection start failed: %s",
                 esp_err_to_name(error));
    }
    return error;
}

static void wifi_task(void *argument)
{
    (void)argument;
    wifi_app_event_t event;

    while (true)
    {
        if (xQueueReceive(wifi_event_queue,
                          &event,
                          portMAX_DELAY) != pdTRUE)
        {
            continue;
        }

        if (event == WIFI_APP_EVENT_GOT_IP)
        {
            continue;
        }

        if (event == WIFI_APP_EVENT_DISCONNECTED)
        {
            vTaskDelay(pdMS_TO_TICKS(WIFI_RETRY_DELAY_MS));
        }

        while (wifi_scan_and_connect() != ESP_OK)
        {
            vTaskDelay(pdMS_TO_TICKS(WIFI_RETRY_DELAY_MS));
        }
    }
}

esp_err_t wifi_app_start(void)
{
    wifi_set_icon(false);

    esp_err_t error = esp_netif_init();
    if (error != ESP_OK && error != ESP_ERR_INVALID_STATE)
    {
        return error;
    }

    error = esp_event_loop_create_default();
    if (error != ESP_OK && error != ESP_ERR_INVALID_STATE)
    {
        return error;
    }

    if (esp_netif_create_default_wifi_sta() == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    wifi_init_config_t wifi_init_config = WIFI_INIT_CONFIG_DEFAULT();
    error = esp_wifi_init(&wifi_init_config);
    if (error != ESP_OK)
    {
        return error;
    }

    error = esp_event_handler_register(WIFI_EVENT,
                                       ESP_EVENT_ANY_ID,
                                       wifi_event_handler,
                                       NULL);
    if (error != ESP_OK)
    {
        return error;
    }

    error = esp_event_handler_register(IP_EVENT,
                                       IP_EVENT_STA_GOT_IP,
                                       wifi_event_handler,
                                       NULL);
    if (error != ESP_OK)
    {
        return error;
    }

    wifi_event_queue = xQueueCreate(WIFI_EVENT_QUEUE_SIZE,
                                    sizeof(wifi_app_event_t));
    if (wifi_event_queue == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    wifi_beep_timer = xTimerCreate("wifi_beep",
                                   pdMS_TO_TICKS(WIFI_BEEP_TIME_MS),
                                   pdFALSE,
                                   NULL,
                                   wifi_beep_timer_callback);
    if (wifi_beep_timer == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    BaseType_t task_created = xTaskCreate(wifi_task,
                                          "wifi_manager",
                                          WIFI_TASK_STACK_SIZE,
                                          NULL,
                                          WIFI_TASK_PRIORITY,
                                          NULL);
    if (task_created != pdPASS)
    {
        return ESP_ERR_NO_MEM;
    }

    error = esp_wifi_set_mode(WIFI_MODE_STA);
    if (error != ESP_OK)
    {
        return error;
    }

    error = esp_wifi_start();
    if (error == ESP_OK)
    {
        ESP_LOGI(TAG, "Wi-Fi station initialized");
    }
    return error;
}

bool wifi_app_is_connected(void)
{
    return wifi_connected;
}

esp_err_t wifi_app_wait_connected(TickType_t timeout_ticks)
{
    TickType_t start_tick = xTaskGetTickCount();

    while (!wifi_app_is_connected())
    {
        if (timeout_ticks != portMAX_DELAY &&
            (xTaskGetTickCount() - start_tick) >= timeout_ticks)
        {
            return ESP_ERR_TIMEOUT;
        }

        vTaskDelay(pdMS_TO_TICKS(100));
    }

    return ESP_OK;
}
