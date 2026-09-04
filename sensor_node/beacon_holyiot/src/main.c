/* main.c - Application main entry point */

/*
 * Copyright (c) 2015-2016 Intel Corporation
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdio.h>
#include <errno.h>
#include <math.h>
#include <zephyr/types.h>
#ifdef CONFIG_BEACON_POWER_TEST_SYSTEM_OFF
#include <zephyr/sys/poweroff.h>
#endif
#include <stddef.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include <zephyr/toolchain.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/device.h>
//#include <zephyr/drivers/i2c.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/pm/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>

#ifdef CONFIG_SOC_NRF54L15_CPUAPP
//#include <nrfx.h>
#endif

#define DEVICE_NAME CONFIG_BT_DEVICE_NAME
#define DEVICE_NAME_LEN (sizeof(DEVICE_NAME) - 1)

/* Sensor data structure - matches CircuitPython format */
struct sensor_data {
    uint8_t protocol_ver;   /* Protocol version */
    uint8_t flags;          /* Protocol flags */
    uint16_t rms_value;     /* RMS × 1000 (little-endian) */
    uint16_t frequency;     /* Spare (currently unused) */
    uint8_t battery_voltage; /* 1.00 V + value × 10 mV */
} __packed;

static struct sensor_data current_sensor_data = {0};

#define SENSOR_PROTOCOL_VERSION 3
#define SENSOR_FLAG_ASSIGNMENT_ACTIVE BIT(0)
#define ASSIGNMENT_HOLD_MS 5000
#define ASSIGNMENT_WINDOW_MS 60000
#define BUTTON_POLL_MS 100

/* LIS2DH accelerometer device */
static const struct device *lis2dh_sensor = DEVICE_DT_GET(DT_NODELABEL(lis2dh));

/* ADC device and configuration */
//static const struct device *adc_dev = DEVICE_DT_GET(DT_NODELABEL(adc));
static const struct adc_dt_spec adc_channel = ADC_DT_SPEC_GET(DT_PATH(zephyr_user));

#define ADC_RESOLUTION 12
#define ADC_GAIN ADC_GAIN_1_4
#define ADC_REFERENCE ADC_REF_INTERNAL  /* Internal reference */
#define ADC_CHANNEL 0

// static struct adc_channel_cfg adc_cfg = {
//     .gain = ADC_GAIN,
//     .reference = ADC_REFERENCE,
//     .acquisition_time = ADC_ACQ_TIME_DEFAULT,
//     .channel_id = ADC_CHANNEL,
// };

static int16_t adc_sample_buffer;
static struct adc_sequence adc_seq = {
    .channels = BIT(ADC_CHANNEL),
    .buffer = &adc_sample_buffer,
    .buffer_size = sizeof(adc_sample_buffer),
    .resolution = ADC_RESOLUTION,
};

/* Custom advertising parameters: ~10 second interval (16000 * 0.625ms = 10000ms) */
static const struct bt_le_adv_param adv_param = {
    .id = BT_ID_DEFAULT,
    .options = BT_LE_ADV_OPT_USE_IDENTITY,
    .interval_min = 16000, /* 10 seconds */
    .interval_max = 16000, /* 10 seconds */
};

/* Get LED device bindings */
static const struct gpio_dt_spec led_red = GPIO_DT_SPEC_GET(DT_NODELABEL(led_red), gpios);
static const struct gpio_dt_spec led_green = GPIO_DT_SPEC_GET(DT_NODELABEL(led_green), gpios);
static const struct gpio_dt_spec led_blue = GPIO_DT_SPEC_GET(DT_NODELABEL(led_blue), gpios);
static const struct gpio_dt_spec assignment_button = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);
static int64_t button_pressed_at;
static int64_t assignment_active_until;
static bool button_was_pressed;

/* LED blink function */
static void blink_led(const struct gpio_dt_spec *led, int times, int delay_ms)
{
    if (device_is_ready(led->port)) 
    {
        int ret = gpio_pin_configure_dt(led, GPIO_OUTPUT_ACTIVE);
        if (ret >= 0) {
            for (int i = 0; i < times; i++) 
            {
                gpio_pin_set_dt(led, 1);
                k_msleep(delay_ms);
                gpio_pin_set_dt(led, 0);
                k_msleep(delay_ms);
            }
        }
        gpio_pin_configure_dt(led, GPIO_OUTPUT_INACTIVE);
    }
}

/* Read battery voltage and encode 1.00-3.55 V in 10 mV steps */
static uint8_t read_battery_voltage(void)
{
    int ret;
    int32_t adc_value;
    int32_t millivolts;
    double voltage;

    if (!adc_is_ready_dt(&adc_channel)) {
        printk("ADC controller devivce %s not ready", adc_channel.dev->name);
        return 0;
    }

    /* Read ADC */
    ret = adc_read(adc_channel.dev, &adc_seq);
	if (ret < 0) {
		printk("Could not read (%d)", ret);
        return 0;
	}

    adc_value = adc_sample_buffer;

    /* Convert ADC value to voltage
     * ADC reads VDD against internal 0.9V reference with 1/4 gain
     * Formula: V_DD = (ADC_value / 4096) * V_ref / gain
     * V_DD = (ADC_value / 4096) * 0.9V * 4
     * V_DD = ADC_value * 3.6 / 4096
     * Then multiply by 2 for some undocumented reason.
     * Also add 0.025V offset to match observed values.
     */
    voltage = (double)adc_value * 3.6 / 4096.0 * 2 + 0.025;
    millivolts = (int32_t)(voltage * 1000.0 + 0.5);
    millivolts = CLAMP(millivolts, 1000, 3550);

    uint8_t encoded_voltage = (uint8_t)((millivolts - 1000 + 5) / 10);
    printk("ADC: %d, Voltage: %.3f V, Battery code: %u\n",
           adc_value, voltage, encoded_voltage);

    return encoded_voltage;
}

/* Custom manufacturer data for sensor values */
#define COMPANY_ID 0xFFFF  /* Use custom/test company ID */

static uint8_t mfg_data[sizeof(uint16_t) + sizeof(struct sensor_data)];

static struct bt_data ad[] = {
    BT_DATA_BYTES(BT_DATA_FLAGS, BT_LE_AD_NO_BREDR),
    BT_DATA(BT_DATA_MANUFACTURER_DATA, mfg_data, sizeof(mfg_data)),
};

/* Set Scan Response data */
static const struct bt_data sd[] = {
    BT_DATA(BT_DATA_NAME_COMPLETE, DEVICE_NAME, DEVICE_NAME_LEN),
};

/* Fetch accelerometer data and compute RMS without DC offset */
#define SAMPLE_RATE_HZ 200
#define SAMPLE_DURATION_MS 100
#define NUM_SAMPLES ((SAMPLE_RATE_HZ * SAMPLE_DURATION_MS) / 1000)  /* 40 samples */

static int fetch_accel_rms(uint16_t *rms_out)
{
    struct sensor_value accel[3];
    double magnitudes[NUM_SAMPLES];
    int rc;
    double sum = 0.0;
    double mean = 0.0;
    double sum_squares = 0.0;

    if (lis2dh_sensor == NULL || !device_is_ready(lis2dh_sensor)) {
        return -ENODEV;
    }

    /* Collect samples for 500ms at 200 Hz */
    for (int i = 0; i < NUM_SAMPLES; i++) {
        rc = sensor_sample_fetch(lis2dh_sensor);
        if (rc == -EBADMSG) {
            /* Sample overrun - ignore in polled mode */
            rc = 0;
            blink_led(&led_red, 1, 10);
        }
        if (rc != 0) {
            printk("Sensor fetch failed: %d\n", rc);
            blink_led(&led_red, 2, 10);
            return rc;
        }

        rc = sensor_channel_get(lis2dh_sensor, SENSOR_CHAN_ACCEL_XYZ, accel);
        if (rc != 0) {
            printk("Sensor channel get failed: %d\n", rc);
            blink_led(&led_red, 1, 10);
            return rc;
        }

        double x = sensor_value_to_double(&accel[0]);
        double y = sensor_value_to_double(&accel[1]);
        double z = sensor_value_to_double(&accel[2]);

        /* Calculate magnitude for this sample */
        magnitudes[i] = sqrt(x*x + y*y + z*z);
        sum += magnitudes[i];

        /* Wait for next sample (1/200Hz = 5ms) */
        k_msleep(5);
    }

    /* Calculate mean (DC offset) */
    mean = sum / NUM_SAMPLES;

    /* Remove DC offset and calculate RMS */
    for (int i = 0; i < NUM_SAMPLES; i++) {
        double ac_component = magnitudes[i] - mean;
        sum_squares += ac_component * ac_component;
    }

    double rms = sqrt(sum_squares / NUM_SAMPLES);
    
    /* Convert to uint16 (RMS * 1000) */
    *rms_out = (uint16_t)(rms * 1000.0);

    printk("Samples: %d, Mean: %.3f, AC RMS: %.3f (coded: %u)\n", 
           NUM_SAMPLES, mean, rms, *rms_out);

    return 0;
}

static void update_advertising_data(void)
{
    /* Prepare manufacturer data */
    mfg_data[0] = COMPANY_ID & 0xFF;
    mfg_data[1] = (COMPANY_ID >> 8) & 0xFF;
    memcpy(&mfg_data[2], &current_sensor_data, sizeof(current_sensor_data));

    /* Push updated payload while advertising */
    bt_le_adv_update_data(ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));
}

static void assignment_button_poll(struct k_timer *timer_id)
{
    int64_t now;
    bool is_pressed;

    ARG_UNUSED(timer_id);

    now = k_uptime_get();
    is_pressed = gpio_pin_get_dt(&assignment_button) > 0;

    if (is_pressed && !button_was_pressed) {
        button_pressed_at = now;
    } else if (!is_pressed && button_was_pressed) {
        if (now - button_pressed_at >= ASSIGNMENT_HOLD_MS) {
            assignment_active_until = now + ASSIGNMENT_WINDOW_MS;
            current_sensor_data.flags |= SENSOR_FLAG_ASSIGNMENT_ACTIVE;
            update_advertising_data();
            blink_led(&led_green, 3, 80);
        }
    }

    button_was_pressed = is_pressed;

    if ((current_sensor_data.flags & SENSOR_FLAG_ASSIGNMENT_ACTIVE) &&
        now >= assignment_active_until) {
        current_sensor_data.flags &= ~SENSOR_FLAG_ASSIGNMENT_ACTIVE;
        update_advertising_data();
    }
}

/* Work item for periodic sensor reading */
static void sensor_work_handler(struct k_work *work);
K_WORK_DEFINE(sensor_work, sensor_work_handler);

/* Timer callback for periodic sensor reading */
static void sensor_timer_callback(struct k_timer *timer_id)
{
    ARG_UNUSED(timer_id);
    k_work_submit(&sensor_work);
}

/* Work handler: do I2C + sensor + BLE update here */
static void sensor_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);
    static uint8_t cycle_count = 0;

    blink_led(&led_blue, 1, 10);

    /* Configure sensor for 200 Hz sampling */
    if (lis2dh_sensor != NULL && device_is_ready(lis2dh_sensor)) {
        struct sensor_value odr_attr;
        odr_attr.val1 = 200;  /* 200 Hz sampling */
        odr_attr.val2 = 0;
        sensor_attr_set(lis2dh_sensor, SENSOR_CHAN_ACCEL_XYZ,
                        SENSOR_ATTR_SAMPLING_FREQUENCY, &odr_attr);
    }

    /* Read accelerometer and compute RMS */
    uint16_t rms;
    if (fetch_accel_rms(&rms) == 0) {
        current_sensor_data.rms_value = rms;
    }

    /* Configure sensor for 0 Hz sampling (power down mode) */
    if (lis2dh_sensor != NULL && device_is_ready(lis2dh_sensor)) {
        struct sensor_value odr_attr;
        odr_attr.val1 = 0;  /* 0 Hz sampling */
        odr_attr.val2 = 0;
        sensor_attr_set(lis2dh_sensor, SENSOR_CHAN_ACCEL_XYZ,
                        SENSOR_ATTR_SAMPLING_FREQUENCY, &odr_attr);
    }

    /* Read battery voltage every 5 minutes (30 cycles * 10s = 300s = 5min) */
    if (cycle_count == 0) {
        current_sensor_data.battery_voltage = read_battery_voltage();
    }
    cycle_count = (cycle_count + 1) % 30;

    /* Update advertising data with new values */
    update_advertising_data();

}

/* Define the timer */
K_TIMER_DEFINE(sensor_timer, sensor_timer_callback, NULL);
K_TIMER_DEFINE(assignment_button_timer, assignment_button_poll, NULL);

static int configure_static_ble_identity(void)
{
    uint8_t device_id[8];
    bt_addr_le_t address = { .type = BT_ADDR_LE_RANDOM };
    ssize_t device_id_length;

    device_id_length = hwinfo_get_device_id(device_id, sizeof(device_id));
    if (device_id_length < 6) {
        return -EIO;
    }

    memcpy(address.a.val, device_id, sizeof(address.a.val));
    address.a.val[5] &= 0x3f;
    address.a.val[5] |= 0xc0;

    return bt_id_reset(BT_ID_DEFAULT, &address, NULL);
}

static void bt_ready(int err)
{
    char addr_s[BT_ADDR_LE_STR_LEN];
    bt_addr_le_t addr = {0};
    size_t count = 1;

    if (err) {
        printk("Bluetooth init failed (err %d)\n", err);
        return;
    }

    printk("Bluetooth initialized\n");

    /* Set up sensor data with defaults */
    current_sensor_data.protocol_ver = SENSOR_PROTOCOL_VERSION;
    current_sensor_data.flags = 0;
    current_sensor_data.rms_value = 0;
    current_sensor_data.frequency = 0;
    current_sensor_data.battery_voltage = read_battery_voltage();
    update_advertising_data();

    /* Read accelerometer once on startup */
    k_work_submit(&sensor_work);

    /* Start advertising */
    err = bt_le_adv_start(&adv_param, ad, ARRAY_SIZE(ad),
                  sd, ARRAY_SIZE(sd));
    if (err) {
        printk("Advertising failed to start (err %d)\n", err);
        return;
    }

    bt_id_get(&addr, &count);
    bt_addr_le_to_str(&addr, addr_s, sizeof(addr_s));

    printk("Beacon started, advertising as %s\n", addr_s);
          printk("Sensor data: proto=%d, flags=%d, rms=%d, battery_code=%d\n",
              current_sensor_data.protocol_ver, current_sensor_data.flags,
              current_sensor_data.rms_value,
            current_sensor_data.battery_voltage);


    
    /* Start periodic sensor reading (10 seconds = 10000 ms) */
    k_timer_start(&sensor_timer, K_MSEC(10000), K_MSEC(10000));
    k_timer_start(&assignment_button_timer, K_MSEC(BUTTON_POLL_MS),
                  K_MSEC(BUTTON_POLL_MS));
}

int main(void)
{
    uint32_t ret;

    if (!device_is_ready(assignment_button.port)) {
        return -ENODEV;
    }

    ret = gpio_pin_configure_dt(&assignment_button, GPIO_INPUT);
    if (ret < 0) {
        return ret;
    }

    ret = configure_static_ble_identity();
    if (ret < 0) {
        return ret;
    }

    /* Configure RAM power control for lower idle power
     * In System ON mode, use CONTROL register to manage RAM sections
     * Enable section 0 (32 KB total), disable the rest
     * Power[n] n = 0 for memory blocks 0 to 31 and n = 1 for memory blocks 32 to 63.
     */
    NRF_MEMCONF->POWER[0].CONTROL = 0x0001;  /* Enable only RAM section 0*/
    NRF_MEMCONF->POWER[1].CONTROL = 0xFFFF;  /* Enable high blocks, disabling causes crash after 60s, idle power still 1.25 uA */

#ifdef CONFIG_BEACON_POWER_TEST_NO_BT
    blink_led(&led_green, 2, 500);
#endif

    //printk("Starting Beacon Demo\n");

    static const struct adc_dt_spec adc_channel = ADC_DT_SPEC_GET(DT_PATH(zephyr_user));

    if (!adc_is_ready_dt(&adc_channel)) 
    {
        printk("ADC controller devivce %s not ready", adc_channel.dev->name);
        blink_led(&led_red, 3, 250);
    }
    else 
    {
        ret = adc_channel_setup_dt(&adc_channel);
        if (ret < 0) 
        {
            printk("Could not setup channel #%d (%d)", 0, ret);
            blink_led(&led_red, 4, 250);
        }
        ret = adc_sequence_init_dt(&adc_channel, &adc_seq);
        if (ret < 0) 
        {
            printk("Could not initalize sequence for channel #%d (%d)", 0, ret);
            blink_led(&led_red, 5, 250);
        }
    }

    if (device_is_ready(lis2dh_sensor)) {
        blink_led(&led_green, 2, 250);
    } else {
        blink_led(&led_red, 3, 250);
    }

#ifdef CONFIG_BEACON_POWER_TEST_NO_BT
    if (device_is_ready(lis2dh_sensor)) {
        const struct sensor_value power_down_odr = {
            .val1 = 0,
            .val2 = 0,
        };

        sensor_attr_set(lis2dh_sensor, SENSOR_CHAN_ACCEL_XYZ,
                        SENSOR_ATTR_SAMPLING_FREQUENCY, &power_down_odr);
    }

#ifdef CONFIG_BEACON_POWER_TEST_SYSTEM_OFF
    k_msleep(20);
    sys_poweroff();
#endif

    return 0;
#endif

    /* Initialize the Bluetooth Subsystem */
    ret = bt_enable(bt_ready);
    if (ret) 
    {
        printk("Bluetooth init failed (err %d)\n", ret);
        blink_led(&led_red, 3, 300);
    }
    return 0;
}
