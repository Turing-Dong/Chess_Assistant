#ifndef __PCA9555A_H
#define __PCA9555A_H

#include "driver/gpio.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "iic.h"

/* PCA9555A I2C connection */
#define PCA9555A_I2C_PORT              I2C_NUM_0
#define PCA9555A_SDA_GPIO              GPIO_NUM_41
#define PCA9555A_SCL_GPIO              GPIO_NUM_42
#define PCA9555A_INT_GPIO              GPIO_NUM_46

/* PCA9555A register map */
#define PCA9555A_INPUT_PORT0_REG       0x00
#define PCA9555A_INPUT_PORT1_REG       0x01
#define PCA9555A_OUTPUT_PORT0_REG      0x02
#define PCA9555A_OUTPUT_PORT1_REG      0x03
#define PCA9555A_INVERSION_PORT0_REG   0x04
#define PCA9555A_INVERSION_PORT1_REG   0x05
#define PCA9555A_CONFIG_PORT0_REG      0x06
#define PCA9555A_CONFIG_PORT1_REG      0x07

/* 7-bit I2C address when A2/A1/A0 are tied low. */
#define PCA9555A_ADDR                  0x20

/* PCA9555A bit masks */
#define PCA9555A_PIN_P0_0              0x0001
#define PCA9555A_PIN_P0_1              0x0002
#define PCA9555A_PIN_P0_2              0x0004
#define PCA9555A_PIN_P0_3              0x0008
#define PCA9555A_PIN_P0_4              0x0010
#define PCA9555A_PIN_P0_5              0x0020
#define PCA9555A_PIN_P0_6              0x0040
#define PCA9555A_PIN_P0_7              0x0080
#define PCA9555A_PIN_P1_0              0x0100
#define PCA9555A_PIN_P1_1              0x0200
#define PCA9555A_PIN_P1_2              0x0400
#define PCA9555A_PIN_P1_3              0x0800
#define PCA9555A_PIN_P1_4              0x1000
#define PCA9555A_PIN_P1_5              0x2000
#define PCA9555A_PIN_P1_6              0x4000
#define PCA9555A_PIN_P1_7              0x8000

/* Board function mapping */
#define PCA9555A_BEEP_IO               PCA9555A_PIN_P0_0
#define PCA9555A_OV5640_PWDN_IO        PCA9555A_PIN_P0_1
#define PCA9555A_OV5640_RESET_IO       PCA9555A_PIN_P0_2
#define PCA9555A_KEY1_IO               PCA9555A_PIN_P0_3
#define PCA9555A_KEY2_IO               0x0000 /* Not connected. */
#define PCA9555A_KEY3_IO               0x0000 /* Not connected. */
#define PCA9555A_KEY4_IO               0x0000 /* Not connected. */
#define PCA9555A_LED_IO                PCA9555A_PIN_P0_4

/* Reserved pins for later board functions. */
#define PCA9555A_RESERVED_P1_0_IO      PCA9555A_PIN_P1_0
#define PCA9555A_RESERVED_P1_1_IO      PCA9555A_PIN_P1_1
#define PCA9555A_RESERVED_P1_2_IO      PCA9555A_PIN_P1_2
#define PCA9555A_RESERVED_P1_3_IO      PCA9555A_PIN_P1_3
#define PCA9555A_RESERVED_P1_4_IO      PCA9555A_PIN_P1_4
#define PCA9555A_RESERVED_P1_5_IO      PCA9555A_PIN_P1_5
#define PCA9555A_RESERVED_P1_6_IO      PCA9555A_PIN_P1_6
#define PCA9555A_RESERVED_P1_7_IO      PCA9555A_PIN_P1_7

/* Configuration bit: 1=input, 0=output. P0_3 is the low-active key; unused pins remain inputs. */
#define PCA9555A_DEFAULT_CONFIG        0xFFE8
#define PCA9555A_DEFAULT_OUTPUT        (PCA9555A_OV5640_RESET_IO | PCA9555A_BEEP_IO)

#define PCA9555A_KEY1                  pca9555a_pin_read(PCA9555A_KEY1_IO)
#define PCA9555A_KEY2                  1 /* Unconnected key: released. */
#define PCA9555A_KEY3                  1 /* Unconnected key: released. */
#define PCA9555A_KEY4                  1 /* Unconnected key: released. */

#define PCA9555A_KEY1_PRES             1
#define PCA9555A_KEY2_PRES             2
#define PCA9555A_KEY3_PRES             3
#define PCA9555A_KEY4_PRES             4

void pca9555a_init(i2c_obj_t self);
esp_err_t pca9555a_read_registers(uint8_t reg, uint8_t *data, size_t len);
esp_err_t pca9555a_read_ports(uint8_t *data, size_t len);
esp_err_t pca9555a_write_ports(uint8_t reg, uint8_t *data, size_t len);
uint16_t pca9555a_ioconfig(uint16_t config_value);
uint16_t pca9555a_pin_write(uint16_t pin, int val);
esp_err_t pca9555a_pin_write_checked(uint16_t pin, int val);
int pca9555a_pin_read(uint16_t pin);
uint8_t pca9555a_key_scan(uint8_t mode);
int pca9555a_int_read(void);
uint16_t pca9555a_beep_write(int val);
uint16_t pca9555a_ov5640_pwdn_write(int val);
uint16_t pca9555a_ov5640_reset_write(int val);
uint16_t pca9555a_led_write(int val);
int pca9555a_key1_read(void);
int pca9555a_key2_read(void);
int pca9555a_key3_read(void);
int pca9555a_key4_read(void);

/*
 * Compatibility aliases for modules that still use the old XL9555 names.
 * Keep new code on the PCA9555A_* and pca9555a_* names above.
 */
#define BEEP_IO                        PCA9555A_BEEP_IO
#define OV_PWDN_IO                     PCA9555A_OV5640_PWDN_IO
#define OV_RESET_IO                    PCA9555A_OV5640_RESET_IO
#define GBC_LED_IO                     PCA9555A_LED_IO
#define KEY0_IO                        PCA9555A_KEY1_IO
#define KEY1_IO                        PCA9555A_KEY2_IO
#define KEY2_IO                        PCA9555A_KEY3_IO
#define KEY3_IO                        PCA9555A_KEY4_IO
#define SLCD_PWR_IO                    PCA9555A_RESERVED_P1_0_IO
#define SLCD_RST_IO                    PCA9555A_RESERVED_P1_1_IO

#define KEY0                           PCA9555A_KEY1
#define KEY1                           PCA9555A_KEY2
#define KEY2                           PCA9555A_KEY3
#define KEY3                           PCA9555A_KEY4
#define KEY0_PRES                      PCA9555A_KEY1_PRES
#define KEY1_PRES                      PCA9555A_KEY2_PRES
#define KEY2_PRES                      PCA9555A_KEY3_PRES
#define KEY3_PRES                      PCA9555A_KEY4_PRES

#define xl9555_init                    pca9555a_init
#define xl9555_read_byte               pca9555a_read_ports
#define xl9555_write_byte              pca9555a_write_ports
#define xl9555_ioconfig                pca9555a_ioconfig
#define xl9555_pin_write               pca9555a_pin_write
#define xl9555_pin_read                pca9555a_pin_read
#define xl9555_key_scan                pca9555a_key_scan

#endif
