// GIGA R1 + GIGA Display Shield demo firmware: an animation drawn with Arduino_GigaDisplay_GFX (Adafruit GFX into an
// SDRAM back buffer, DMA2D copy into the LTDC frame buffer, ST7701 over DSI). Landscape 800x480: a bouncing ball, a
// rotating wheel, a scrolling sine trace and a frame counter. Serial1 (USART1) logs "frame=N t=MS" every 25 frames.
// Build: arduino-cli compile -b arduino:mbed_giga:giga --output-dir build .
// Copyright (c) 2026 embeddyn contributors. MIT License.
#include "Arduino_GigaDisplay_GFX.h"

GigaDisplay_GFX display;

static const uint16_t BG = 0x0841;   // near black
static int16_t ballX = 120, ballY = 140, dx = 9, dy = 6;
static const int16_t R = 28;
static uint32_t frame = 0;
static int16_t wave[200];

void setup() {
  Serial1.begin(115200);
  display.begin();
  display.setRotation(1);
  for (int i = 0; i < 200; i++) wave[i] = 0;
  Serial1.println("display demo start");
}

static void drawWheel(int16_t cx, int16_t cy, int16_t r, float a) {
  display.drawCircle(cx, cy, r, 0xFFFF);
  for (int k = 0; k < 6; k++) {
    float t = a + k * PI / 3;
    uint16_t c = display.color565(80 + 30 * k, 200 - 25 * k, 255);
    display.drawLine(cx, cy, cx + r * cos(t), cy + r * sin(t), c);
  }
  display.fillCircle(cx + r * cos(a), cy + r * sin(a), 8, display.color565(255, 200, 0));
}

void loop() {
  display.startBuffering();
  display.fillScreen(BG);

  // title bar
  display.fillRect(0, 0, 800, 44, display.color565(40, 60, 120));
  display.setTextColor(0xFFFF);
  display.setTextSize(3);
  display.setCursor(14, 11);
  display.print("embeddyn  GIGA R1 + Display Shield");

  // bouncing ball
  ballX += dx; ballY += dy;
  if (ballX < R || ballX > 800 - R) { dx = -dx; ballX += 2 * dx; }
  if (ballY < 44 + R || ballY > 330 - R) { dy = -dy; ballY += 2 * dy; }
  display.fillCircle(ballX, ballY, R, display.color565(255, 60 + (frame * 7) % 180, 60));
  display.drawCircle(ballX, ballY, R, 0xFFFF);

  // rotating wheel
  drawWheel(680, 190, 90, frame * 0.15f);

  // scrolling sine trace
  for (int i = 0; i < 199; i++) wave[i] = wave[i + 1];
  wave[199] = (int16_t)(50 * sin(frame * 0.25f) + 20 * sin(frame * 0.07f));
  display.drawFastHLine(0, 405, 800, display.color565(70, 70, 70));
  for (int i = 0; i < 199; i++) {
    display.drawLine(i * 4, 405 - wave[i], (i + 1) * 4, 405 - wave[i + 1], display.color565(60, 255, 120));
  }

  // frame counter and uptime
  display.setTextSize(2);
  display.setTextColor(display.color565(255, 214, 10));
  display.setCursor(14, 452);
  display.print("frame ");
  display.print(frame);
  display.print("   t = ");
  display.print(millis());
  display.print(" ms");

  display.endBuffering();
  if (frame % 25 == 0) {
    Serial1.print("frame=");
    Serial1.print(frame);
    Serial1.print(" t=");
    Serial1.println(millis());
  }
  frame++;
  delay(20);
}
