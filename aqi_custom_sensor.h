#include "esphome.h"
#include "Adafruit_PM25AQI.h"

class PMSA003I: public PollingComponent {
public:
    Sensor* pm25= new Sensor();
    Sensor* pm10 = new Sensor();
    Sensor* pm100 = new Sensor();

     // constructor
    PMSA003I() : PollingComponent(3000) {}
    void setup() override {
        TryInitialize();
    }

    void update() override {
        TryInitialize();

        PM25_AQI_Data data;
        if (!aqi.read(&data))
        {
            ESP_LOGE("sensor", "Could not read AQI");
            return;
        }

        pm10->publish_state(data.pm10_standard);
        pm25->publish_state(data.pm25_standard);
        pm100->publish_state(data.pm100_standard);
    }

private:
    Adafruit_PM25AQI aqi;
    bool initialized = false;

    void TryInitialize()
    {
        if (initialized)
        {
            return;
        }

        if (!aqi.begin_I2C())
        {
            ESP_LOGE("sensor", "Could not initialize AQI, will try again");
        }
        else
        {
            initialized = true;
            ESP_LOGI("sensor", "Succesfully initialized AQI");
        }
    }

};

