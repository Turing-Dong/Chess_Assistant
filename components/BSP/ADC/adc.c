#include "adc.h"

#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"

static adc_oneshot_unit_handle_t light_adc_handle;
static adc_cali_handle_t light_adc_cali_handle;
static adc_unit_t light_adc_unit;
static adc_channel_t light_adc_channel;
static bool light_adc_initialized;
static bool light_adc_cali_enabled;

static esp_err_t light_adc_cali_init(void)
{
    esp_err_t ret = ESP_ERR_NOT_SUPPORTED;

#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t cali_config = {
        .unit_id = light_adc_unit,
        .chan = light_adc_channel,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };

    ret = adc_cali_create_scheme_curve_fitting(&cali_config, &light_adc_cali_handle);
#elif ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    adc_cali_line_fitting_config_t cali_config = {
        .unit_id = light_adc_unit,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };

    ret = adc_cali_create_scheme_line_fitting(&cali_config, &light_adc_cali_handle);
#endif

    light_adc_cali_enabled = (ret == ESP_OK);

    return ret;
}

esp_err_t light_adc_init(void)
{
    if (LIGHT_ADC_GPIO == GPIO_NUM_NC)
    {
        return ESP_ERR_NOT_SUPPORTED;
    }

    if (light_adc_initialized)
    {
        return ESP_OK;
    }

    esp_err_t ret = adc_oneshot_io_to_channel(LIGHT_ADC_GPIO, &light_adc_unit, &light_adc_channel);
    if (ret != ESP_OK)
    {
        return ret;
    }

    adc_oneshot_unit_init_cfg_t unit_config = {
        .unit_id = light_adc_unit,
    };

    ret = adc_oneshot_new_unit(&unit_config, &light_adc_handle);
    if (ret != ESP_OK)
    {
        return ret;
    }

    adc_oneshot_chan_cfg_t channel_config = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };

    ret = adc_oneshot_config_channel(light_adc_handle, light_adc_channel, &channel_config);
    if (ret != ESP_OK)
    {
        return ret;
    }

    light_adc_cali_init();
    light_adc_initialized = true;

    return ESP_OK;
}

esp_err_t light_adc_read_raw(int *raw)
{
    if (raw == NULL)
    {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t ret = light_adc_init();
    if (ret != ESP_OK)
    {
        return ret;
    }

    return adc_oneshot_read(light_adc_handle, light_adc_channel, raw);
}

esp_err_t light_adc_read_voltage_mv(int *voltage_mv)
{
    if (voltage_mv == NULL)
    {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t ret = light_adc_init();
    if (ret != ESP_OK)
    {
        return ret;
    }

    if (light_adc_cali_enabled)
    {
        return adc_oneshot_get_calibrated_result(light_adc_handle,
                                                 light_adc_cali_handle,
                                                 light_adc_channel,
                                                 voltage_mv);
    }

    int raw;
    ret = light_adc_read_raw(&raw);
    if (ret != ESP_OK)
    {
        return ret;
    }

    *voltage_mv = raw * 3300 / LIGHT_ADC_MAX_RAW;

    return ESP_OK;
}

esp_err_t light_adc_read_percent(int *percent)
{
    if (percent == NULL)
    {
        return ESP_ERR_INVALID_ARG;
    }

    int raw;
    esp_err_t ret = light_adc_read_raw(&raw);
    if (ret != ESP_OK)
    {
        return ret;
    }

    *percent = raw * 100 / LIGHT_ADC_MAX_RAW;

    return ESP_OK;
}
