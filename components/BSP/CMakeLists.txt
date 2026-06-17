set(src_dirs
            CAMERA
            LED
            BEEP
            ADC
            IIC
            KEY
            LCD
            PCA9555A
            SPI)

set(include_dirs
            CAMERA
            LED
            BEEP
            ADC
            IIC
            KEY
            LCD
            PCA9555A
            SPI)

set(requires
            driver
            esp_adc
            esp32-camera)

idf_component_register(SRC_DIRS ${src_dirs} INCLUDE_DIRS ${include_dirs} REQUIRES ${requires})

component_compile_options(-ffast-math -O3 -Wno-error=format=-Wno-format)
