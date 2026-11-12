// for controlling the 16 bit dac

// pin connections to the Teensy
//  AD5686R - Teensy
//  3 (VoutA)   - 14 (A0)(to check the output)
//  4 GND - GND
//  5 (VDD) - 3.3V
//  8 (SDO) - 12 (MISO)
//  9 LDAC  - GND *can be changed, will need to change code if this were to be changed 
//  10 GAIN - GND
//  11 Vlogic - 3.3V
//  12 SCLK - 13 SCK
//  13 SYNC' - 10 CS
//  14 (SDIN) - 11 MOSI
//  15 RESET' - 8 (digital pin)
//  16 RSTSEL - 3.3V set to do midscale for DAC startup 

// communication is through SPI where SDO, SCLK, SYNC', and SDIN are MISO, SCL, CS, and MOSI on the teensy
// SPI is in MODE1

// input shift register structure:
// MSB: xxxx - yyyy - zzzz_zzzz_zzzz_zzzz LSB
// x = command bits C3-C0
// y = selected dac channel - DAC D - DAC A
// z = data bit-word, 16 bits of data

// this operation shouldn't be daisy chaining, so this functionality was not implemented

#include <Arduino.h>
#include <SPI.h>
#include "AD5686.h"

const int SYNC = 10; // CS on Teensy
const int RESET = 8; // active low
const int GAIN = 1;  // can be 1 or 2 *update dependent on wiring **this probably does not need to be in the code (note the teensy can't measure gain of 2 without breaking a port)

AD5686 dac(SYNC, RESET);

// testing on the Teensy
const int ADC_pin = A0;


void setup()
{
    Serial.begin(115200);
    while (!Serial && millis() < 3000); // wait for serial monitor

    dac.begin();
   
    analogReadResolution(12);
    analogReadAveraging(16);

    dac.softwareResetDAC();
}


void loop()
{
    //test script to make sure dac is working with using A0 testing on teensy goes through range of codes for 0%, 25%, 50%, 75%, and ~100% of 2.5V, and which is applied to all channels 
    uint16_t testCodes[] = {0, 16384, 32768, 49152, 65535};

    Serial.println("--- Starting DAC Verification ---");
    for (int i = 0; i < 5; i++)
    {
        uint16_t code = testCodes[i];

        dac.setDACVoltage(dac.CH_ALL, code);
        delay(10);
        int adcRaw = analogRead(ADC_pin);
        float measuredVolts = (adcRaw / 4095.0) * 3.3;
        float expectedVolts = (code / 65535.0) * 2.5;
        float errorMv = (measuredVolts - expectedVolts) * 1000.0;

        Serial.printf("Code: %5u | Expected: %.3f V | Measured: %.3f V | Error: %+.1f mV\n",
                      code, expectedVolts, measuredVolts, errorMv);
    }
    Serial.println();
    delay(3000);
}