#include "pca9555a.h"

static const char *TAG = "PCA9555A";

i2c_obj_t pca9555a_i2c_master;
static uint16_t pca9555a_output_latch = PCA9555A_DEFAULT_OUTPUT;
static uint16_t pca9555a_failed = 0;

esp_err_t pca9555a_read_registers(uint8_t reg, uint8_t *data, size_t len)
{
    i2c_buf_t bufs[2] = {
        {.len = 1, .buf = &reg},
        {.len = len, .buf = data},
    };

    return i2c_transfer(&pca9555a_i2c_master, PCA9555A_ADDR, 2, bufs,
                        I2C_FLAG_WRITE | I2C_FLAG_READ | I2C_FLAG_STOP);
}

esp_err_t pca9555a_read_ports(uint8_t *data, size_t len)
{
    return pca9555a_read_registers(PCA9555A_INPUT_PORT0_REG, data, len);
}

esp_err_t pca9555a_write_ports(uint8_t reg, uint8_t *data, size_t len)
{
    i2c_buf_t bufs[2] = {
        {.len = 1, .buf = &reg},
        {.len = len, .buf = data},
    };

    return i2c_transfer(&pca9555a_i2c_master, PCA9555A_ADDR, 2, bufs, I2C_FLAG_STOP);
}

static void pca9555a_int_gpio_init(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = 1ULL << PCA9555A_INT_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    gpio_config(&io_conf);
}

static esp_err_t pca9555a_write_u16(uint8_t reg, uint16_t value)
{
    uint8_t data[2] = {
        (uint8_t)(value & 0xFF),
        (uint8_t)((value >> 8) & 0xFF),
    };

    return pca9555a_write_ports(reg, data, 2);
}

uint16_t pca9555a_pin_write(uint16_t pin, int val)
{
    if (val)
    {
        pca9555a_output_latch |= pin;
    }
    else
    {
        pca9555a_output_latch &= (uint16_t)~pin;
    }

    pca9555a_write_u16(PCA9555A_OUTPUT_PORT0_REG, pca9555a_output_latch);

    return pca9555a_output_latch;
}

int pca9555a_pin_read(uint16_t pin)
{
    uint8_t r_data[2] = {0};
    uint16_t ret;

    pca9555a_read_ports(r_data, 2);
    ret = ((uint16_t)r_data[1] << 8) | r_data[0];

    return (ret & pin) ? 1 : 0;
}

uint16_t pca9555a_ioconfig(uint16_t config_value)
{
    esp_err_t err;
    int retry = 3;

    do
    {
        err = pca9555a_write_u16(PCA9555A_CONFIG_PORT0_REG, config_value);

        if (err != ESP_OK)
        {
            retry--;
            pca9555a_failed = 1;
            ESP_LOGE(TAG, "%s configure 0x%04X failed, ret: %d", __func__, config_value, err);
            vTaskDelay(pdMS_TO_TICKS(100));

            if ((retry <= 0) && pca9555a_failed)
            {
                vTaskDelay(pdMS_TO_TICKS(5000));
                esp_restart();
            }
        }
        else
        {
            pca9555a_failed = 0;
            break;
        }
    } while (retry);

    return config_value;
}

void pca9555a_init(i2c_obj_t self)
{
    uint8_t r_data[2] = {0};

    pca9555a_int_gpio_init();

    if (self.init_flag == ESP_FAIL)
    {
        self = iic_init(PCA9555A_I2C_PORT);
    }

    pca9555a_i2c_master = self;

    /* Clear the INT state once after power-on by reading the input ports. */
    pca9555a_read_ports(r_data, 2);

    pca9555a_output_latch = PCA9555A_DEFAULT_OUTPUT;
    pca9555a_write_u16(PCA9555A_OUTPUT_PORT0_REG, pca9555a_output_latch);
    pca9555a_write_u16(PCA9555A_INVERSION_PORT0_REG, 0x0000);
    pca9555a_ioconfig(PCA9555A_DEFAULT_CONFIG);
}

uint8_t pca9555a_key_scan(uint8_t mode)
{
    uint8_t keyval = 0;
    static uint8_t key_up = 1;

    if (mode)
    {
        key_up = 1;
    }

    if (key_up && (PCA9555A_KEY1 == 0 || PCA9555A_KEY2 == 0 ||
                   PCA9555A_KEY3 == 0 || PCA9555A_KEY4 == 0))
    {
        vTaskDelay(pdMS_TO_TICKS(10));
        key_up = 0;

        if (PCA9555A_KEY1 == 0)
        {
            keyval = PCA9555A_KEY1_PRES;
        }
        else if (PCA9555A_KEY2 == 0)
        {
            keyval = PCA9555A_KEY2_PRES;
        }
        else if (PCA9555A_KEY3 == 0)
        {
            keyval = PCA9555A_KEY3_PRES;
        }
        else if (PCA9555A_KEY4 == 0)
        {
            keyval = PCA9555A_KEY4_PRES;
        }
    }
    else if (PCA9555A_KEY1 == 1 && PCA9555A_KEY2 == 1 &&
             PCA9555A_KEY3 == 1 && PCA9555A_KEY4 == 1)
    {
        key_up = 1;
    }

    return keyval;
}

int pca9555a_int_read(void)
{
    return gpio_get_level(PCA9555A_INT_GPIO);
}

uint16_t pca9555a_beep_write(int val)
{
    return pca9555a_pin_write(PCA9555A_BEEP_IO, val);
}

uint16_t pca9555a_ov5640_pwdn_write(int val)
{
    return pca9555a_pin_write(PCA9555A_OV5640_PWDN_IO, val);
}

uint16_t pca9555a_ov5640_reset_write(int val)
{
    return pca9555a_pin_write(PCA9555A_OV5640_RESET_IO, val);
}

uint16_t pca9555a_led_write(int val)
{
    return pca9555a_pin_write(PCA9555A_LED_IO, val);
}

int pca9555a_key1_read(void)
{
    return pca9555a_pin_read(PCA9555A_KEY1_IO);
}

int pca9555a_key2_read(void)
{
    return pca9555a_pin_read(PCA9555A_KEY2_IO);
}

int pca9555a_key3_read(void)
{
    return pca9555a_pin_read(PCA9555A_KEY3_IO);
}

int pca9555a_key4_read(void)
{
    return pca9555a_pin_read(PCA9555A_KEY4_IO);
}
