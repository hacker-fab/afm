#ifndef AD5686_H
#define AD5686_H

#include <Arduino.h>
#include <SPI.h>

class AD5686{
  public:

  // commands
  enum DAC_COMMAND : uint8_t
  {
      CMD_NO_OP = 0x0,                   // 0000: No operation
      CMD_WRITE_INPUT_REG = 0x01,        // 0001: Write to Input Register n (dependent on LDAC) **note the diagram on page 23 of the datasheet to understand the buffering here
      CMD_UPDATE_DAC_REG = 0x02,         // 0010: Update DAC Register n with contents of Input Register n
      CMD_WRITE_UPDATE = 0x03,           // 0011: Write to and update DAC Channel n
      CMD_POWER_TOGGLE = 0x04,           // 0100: Power down/power up DAC
      CMD_LDAC_MASK_REG = 0x05,          // 0101: Hardware LDAC mask register
      CMD_SW_RESET = 0x06,               // 0110: Software reset (power-on reset)
      CMD_INTERNAL_REF_SETUP_REG = 0x07, // 0111: Internal reference setup register **on by default at power-up
      CMD_SETUP_DCEN_REG = 0x08,         // 1000: Set up DCEN register (daisy-chain enable) **don't think this is needed for our application
      CMD_SETUP_READBACK_REG = 0x09,     // 1001: Set up readback register (readback enable)
      CMD_NO_OP_DAISY = 0xA              // 1111: No operation, daisy - chain mode **don't think this is needed for our application
  };

  // channels (can combine using | )
  enum DAC_CHANNEL : uint8_t
  {
      CH_NONE = 0x0, // for commands like reset
      CH_A = 0x1,
      CH_B = 0x2,
      CH_C = 0x4,
      CH_D = 0x8,
      CH_ALL = 0xF // all the DACs
  };

  AD5686(uint8_t syncPin, uint8_t resetPin, uint32_t SPIClockHz = 10000000): _syncPin(syncPin), _resetPin(resetPin), _SPISettings(SPIClockHz, MSBFIRST, SPI_MODE1) {}

  void begin(){
     pinMode(_syncPin, OUTPUT);
      digitalWrite(_syncPin, HIGH);

      pinMode(_resetPin, OUTPUT);
      digitalWrite(_resetPin, HIGH);

      SPI.begin();
  }


  // function that does everything
  void writeDAC(uint8_t command, uint8_t channel, uint16_t data)
  {
      // need to send as 3, 8 bit transmissions with SYNC dropped the whole time
      uint8_t MSB = ((command & 0x0F) << 4) | (channel & 0x0F);
      uint8_t upper_data = (data >> 8) & 0xFF;
      uint8_t lower_data = data & 0xFF;

      SPI.beginTransaction(_SPISettings);

      digitalWrite(_syncPin, LOW);

      delayNanoseconds(25);

      SPI.transfer(MSB);
      SPI.transfer(upper_data);
      SPI.transfer(lower_data);

      delayNanoseconds(25);
      digitalWrite(_syncPin, HIGH);

      SPI.endTransaction();
  }

  // shortcuts for the functions that we may/will use
  void setDACVoltage(uint8_t channels, uint16_t data)
  {
      writeDAC(CMD_WRITE_UPDATE, channels, data);
  }

  void softwareResetDAC()
  {
      writeDAC(CMD_SW_RESET, CH_NONE, 0x0000);
  }

  void pinResetTeensy(){ //**not tested
      digitalWrite(_resetPin, LOW); //reset by dropping the pin
      delayMicroseconds(10);
      digitalWrite(_resetPin, HIGH); 
      delayMicroseconds(100);
  }

  private: 
  uint8_t _syncPin;
  uint8_t _resetPin;
  SPISettings _SPISettings;
};

#endif