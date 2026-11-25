// Focus Sprint e-paper display — Waveshare ESP32-S3-ePaper-1.54 (SSD1681, 200x200)
// Draws what focus.py / FocusInk.app on the Mac sends over BLE and counts down locally.
// The Mac decides all text; this firmware only picks one of three layouts:
//   list (idle "3 things", day summary), timed (task + timer + progress), untimed (task only).
// Buttons only report events; the Mac decides what they mean:
//   Complete    (upper, ✓, BOOT): short = "boot", held 1 s = "boot_long"
//   Start/Pause (lower, ▶‖, PWR): short = "pwr", double = "pwr_double"  (holding PWR is left alone:
//   Waveshare uses it for battery power-off)
// Power: battery icon when unplugged; on battery the board deep-sleeps when idle or
// disconnected for a while, keeping the last screen. Either button wakes it.

#include <BLE.h>  // arduino-esp32 core 4.x BLE API
#include <GxEPD2_BW.h>
#include <Fonts/FreeSans9pt7b.h>
#include <Fonts/FreeSansBold9pt7b.h>
#include <Fonts/FreeSansBold12pt7b.h>
#include <Fonts/FreeSansBold18pt7b.h>
#include <Fonts/FreeSansBold24pt7b.h>
#include "esp_sleep.h"
#include "esp_task_wdt.h"
#include "driver/rtc_io.h"

// Pins from Waveshare's own examples (02_Example/Arduino/*/user_config.h)
#define EPD_DC 10
#define EPD_CS 11
#define EPD_SCK 12
#define EPD_MOSI 13
#define EPD_RST 9
#define EPD_BUSY 8
#define EPD_PWR 6        // active LOW
#define AUDIO_PWR 42     // active LOW; keep off
#define VBAT_PWR 17      // HIGH keeps the board on when running from battery
#define BOOT_BTN 0       // active LOW
#define PWR_BTN 18       // active LOW
#define BAT_ADC 4        // ADC1 channel 3, battery voltage / 2 (01_ADC_Test)

// Deep sleep on battery (minutes)
#define SLEEP_IDLE_MIN 15          // no button press / screen change while not counting down
#define SLEEP_DISCONNECTED_MIN 5   // no Mac connection

#define SERVICE_UUID "6e5a0001-8b3c-4a5e-9f1d-0f0c05f5a001"
#define STATE_UUID   "6e5a0002-8b3c-4a5e-9f1d-0f0c05f5a001"  // Mac -> device (write)
#define EVENT_UUID   "6e5a0003-8b3c-4a5e-9f1d-0f0c05f5a001"  // device -> Mac (notify)

GxEPD2_BW<GxEPD2_154_D67, GxEPD2_154_D67::HEIGHT> display(GxEPD2_154_D67(EPD_CS, EPD_DC, EPD_RST, EPD_BUSY));

struct Metrics { int asc, desc, lineH; };  // font metrics for text boxes (declared early for Arduino prototypes)

// Payload fields (0x1F separated): header, right, body, remaining, total, mode, footer, lines (0x1E separated), interruptions
// mode: run = count down, up = count up from "remaining" (elapsed seconds), static/start/word/prompt = no clock
struct State {
  String header, right, body, mode, footer, lines;
  long remaining = 0, total = 0;   // seconds at receipt; total 0 = no timer
  int ints = 0;
  bool valid = false;
  uint32_t receivedAt = 0;
};

State st;
portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
String pending;
volatile bool hasPending = false;
volatile bool connected = false;
volatile bool subscribed = false;   // the Mac listens for button events
BLECharacteristic eventChar;

// Button presses made while the Mac isn't connected (including the one that woke the board from
// deep sleep) are kept for QUEUE_MAX_MS and delivered as soon as it is.
const int QUEUE_LEN = 4;
const uint32_t QUEUE_MAX_MS = 30000;
const char *queuedEv[QUEUE_LEN];
uint32_t queuedAt[QUEUE_LEN];
volatile int queued = 0;
portMUX_TYPE qmux = portMUX_INITIALIZER_UNLOCKED;

// Advertising: fast for a while after boot, a press or a disconnect so the Mac finds the board in
// a second or two; slow otherwise to save battery.
const uint32_t FAST_ADV_MS = 60000;
volatile uint32_t fastAdvSince = 0;

String lastDrawKey, lastLayoutKey, lastPayload;
int partialCount = 0;
uint32_t lastFullAt = 0, drawKeyChangedAt = 0;
String pendingDrawKey;
// Refresh policy: everything is drawn with fast, flash-free partial refreshes. A full refresh (the
// black/white flash) only clears the ghosting that partials slowly build up: after FULL_EVERY text
// changes, at most once per FULL_MIN_GAP_MS, and only when the text changes (never on a minute tick).
const int FULL_EVERY = 20;
const uint32_t FULL_MIN_GAP_MS = 10UL * 60 * 1000;
const uint32_t SETTLE_MS = 400;  // wait for quick successive updates (task -> Break? -> next) to settle
volatile uint32_t lastActivity = 0;
uint32_t lastConnected = 0;

// Battery
int batteryMv = 0;
bool usbPlugged = true;
uint32_t lastBatteryRead = 0;

void parsePayload(const String &p) {
  String f[9];
  int idx = 0, start = 0;
  for (int i = 0; i <= (int)p.length() && idx < 9; i++) {
    if (i == (int)p.length() || p[i] == '\x1f') { f[idx++] = p.substring(start, i); start = i + 1; }
  }
  st.header = f[0]; st.right = f[1]; st.body = f[2];
  st.remaining = f[3].toInt(); st.total = f[4].toInt();
  st.mode = f[5]; st.footer = f[6]; st.lines = f[7]; st.ints = f[8].toInt();
  st.receivedAt = millis();
  st.valid = true;
}

long remainingNow() {
  if (!st.valid) return 0;
  long secs = (long)((millis() - st.receivedAt) / 1000);
  if (st.mode == "up") return st.remaining + secs;  // count-up: "remaining" holds the elapsed seconds
  if (st.mode != "run") return st.remaining;
  return st.remaining - secs;
}

// ---- battery ---------------------------------------------------------------
void readBattery() {
  long sum = 0;
  for (int i = 0; i < 8; i++) sum += analogReadMilliVolts(BAT_ADC);
  batteryMv = (int)(sum / 8) * 2;
  usbPlugged = HWCDC::isPlugged();
  lastBatteryRead = millis();
}
bool hasBattery() { return batteryMv > 2800; }
int batteryPct() { return constrain((batteryMv - 3300) * 100 / (4150 - 3300), 0, 100); }
bool onBattery() { return hasBattery() && !usbPlugged; }

// ---- drawing helpers -------------------------------------------------------
int textWidth(const String &s) {
  int16_t bx, by; uint16_t bw, bh;
  display.getTextBounds(s, 0, 0, &bx, &by, &bw, &bh);
  return bw;
}

String fitText(String s, int w) {
  if (textWidth(s) <= w) return s;
  while (s.length() > 1 && textWidth(s + "...") > w) s.remove(s.length() - 1);
  return s + "...";
}

// ---- overlap-safe text boxes -------------------------------------------------
// Every block of text gets a box (top..bottom). Line height comes from the current font's real
// glyph bounds, lines that don't fit are dropped and the last visible line ends in "...".
// Nothing is ever drawn below `bottom`, so blocks can't run into each other.
Metrics fontMetrics() {
  int16_t bx, by; uint16_t bw, bh;
  display.getTextBounds("Ahgjy|", 0, 0, &bx, &by, &bw, &bh);
  Metrics m;
  m.asc = -by;
  m.desc = (int)bh + by;
  m.lineH = m.asc + m.desc + 4;
  return m;
}

#define MAX_BOX_LINES 8
int wrapLines(const String &text, int w, int maxLines, String out[]) {
  String words = text + " ";
  String line = "";
  int n = 0, start = 0;
  while (start < (int)words.length()) {
    int sp = words.indexOf(' ', start);
    String word = words.substring(start, sp);
    if (word.length() == 0) { start = sp + 1; continue; }
    String trial = line.length() ? line + " " + word : word;
    if (textWidth(trial) <= w || !line.length()) { line = trial; start = sp + 1; continue; }
    if (n == maxLines - 1) {  // last allowed line: squeeze the rest in with "..."
      String rest = words.substring(start);
      rest.trim();
      out[n++] = fitText(line + " " + rest, w);
      return n;
    }
    out[n++] = fitText(line, w);
    line = "";
  }
  if (line.length() && n < maxLines) out[n++] = fitText(line, w);
  return n;
}

// Draws `text` inside x..x+w, top..bottom. Returns the height used (0 if nothing fits).
int drawBox(const String &text, int x, int top, int w, int bottom, int maxLines = MAX_BOX_LINES,
            bool vcenter = false, bool hcenter = false) {
  if (!text.length()) return 0;
  Metrics m = fontMetrics();
  int fit = (bottom - top + 4) / m.lineH;  // the last line needs no gap below it
  int limit = min(min(maxLines, fit), MAX_BOX_LINES);
  if (limit < 1) return 0;
  String lines[MAX_BOX_LINES];
  int n = wrapLines(text, w, limit, lines);
  int h = n * m.lineH - 4;
  int y = vcenter ? top + (bottom - top - h) / 2 : top;
  for (int i = 0; i < n; i++) {
    int lx = hcenter ? x + (w - textWidth(lines[i])) / 2 : x;
    display.setCursor(lx, y + m.asc + i * m.lineH);
    display.print(lines[i]);
  }
  return h;
}

void drawCentered(const String &s, int y) {
  int16_t bx, by; uint16_t bw, bh;
  display.getTextBounds(s, 0, 0, &bx, &by, &bw, &bh);
  display.setCursor((200 - bw) / 2 - bx, y);
  display.print(s);
}

// Small lightning bolt (interruptions), white on the black header. Returns width used.
int drawBolt(int x, int y) {
  display.fillTriangle(x + 5, y, x, y + 8, x + 5, y + 8, GxEPD_WHITE);
  display.fillTriangle(x + 3, y + 7, x + 8, y + 7, x + 3, y + 15, GxEPD_WHITE);
  return 10;
}

// Battery outline with fill, white on the black header. Returns width used.
int drawBattery(int x, int y) {
  display.drawRect(x, y, 18, 10, GxEPD_WHITE);
  display.fillRect(x + 18, y + 3, 2, 4, GxEPD_WHITE);
  int fill = 14 * batteryPct() / 100;
  if (fill > 0) display.fillRect(x + 2, y + 2, fill, 6, GxEPD_WHITE);
  return 22;
}

String timerText(long rem) {
  if (st.mode == "up") return String(rem / 60) + " min";  // elapsed, rounded down
  if (st.total <= 0) return "";
  if (rem <= 0) return "+" + String((-rem) / 60) + " min";  // overtime
  return String((rem + 59) / 60) + " min";                  // round up: "1 min" in the last minute
}

int lineCount(const String &s) {
  if (!s.length()) return 0;
  int n = 1;
  for (char c : s) if (c == '\x1e') n++;
  return n;
}

void drawHeader() {
  display.fillRect(0, 0, 200, 22, GxEPD_BLACK);
  display.setTextColor(GxEPD_WHITE);
  display.setFont(&FreeSans9pt7b);

  // Right side, drawn right-to-left: battery, position/"*", interruptions
  int x = 196;
  if (onBattery()) { x -= 22; drawBattery(x + 2, 6); }
  String right = st.right + (connected ? "" : " *");
  right.trim();
  if (right.length()) {
    int w = textWidth(right);
    x -= w + 4;
    display.setCursor(x, 16);
    display.print(right);
  }
  if (st.ints > 0) {
    String n = String(st.ints);
    int w = textWidth(n);
    x -= w + 4;
    display.setCursor(x, 16);
    display.print(n);
    x -= 11;
    drawBolt(x, 3);
    x -= 2;
  }

  display.setCursor(4, 16);
  display.print(fitText(st.header, x - 10));
  display.setTextColor(GxEPD_BLACK);
}

void render(bool full, const String &footerOverride = "") {
  long rem = remainingNow();
  if (full) display.setFullWindow(); else display.setPartialWindow(0, 0, 200, 200);
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);

    if (!st.valid) {
      display.setFont(&FreeSansBold12pt7b);
      drawCentered("Focus Sprint", 80);
      display.setFont(&FreeSans9pt7b);
      drawCentered(connected ? "Connected" : "Waiting for Mac...", 120);
      drawCentered(footerOverride.length() ? footerOverride : String("open FocusInk"), 145);
      continue;
    }

    drawHeader();

    // Screen regions: header 0-22, body 28-174, footer 180-199.
    const int BODY_TOP = 28, BODY_BOTTOM = 174;
    String footer = footerOverride.length() ? footerOverride : st.footer;
    display.setFont(&FreeSans9pt7b);
    drawBox(footer, 4, 180, 192, 199, 1);

    if (st.mode == "start" || st.mode == "word" || st.mode == "prompt") {
      // One big word, centred: "Focus?", "Paused..", "Break?". Largest font that fits on one line.
      display.setFont(&FreeSansBold24pt7b);
      if (textWidth(st.body) > 192) display.setFont(&FreeSansBold18pt7b);
      if (textWidth(st.body) > 192) display.setFont(&FreeSansBold12pt7b);
      drawBox(st.body, 4, BODY_TOP, 192, BODY_BOTTOM, 1, true, true);
      continue;
    }

    if (st.mode == "up") {
      // Break: big "Break!" with the time taken so far counting up underneath.
      display.setFont(&FreeSansBold24pt7b);
      if (textWidth(st.body) > 192) display.setFont(&FreeSansBold18pt7b);
      drawBox(st.body, 4, BODY_TOP, 192, 118, 1, true, true);
      display.setFont(&FreeSansBold18pt7b);
      drawCentered(timerText(rem), 152);
      continue;
    }

    int n = lineCount(st.lines);
    if (n > 0) {
      // List: idle "3 things" or the day summary. Each item gets an equal slot.
      display.setFont(&FreeSansBold9pt7b);
      int slot = (BODY_BOTTOM - BODY_TOP) / n;
      int y = BODY_TOP + 4, start = 0;
      for (int i = 0; i < n; i++) {
        int end = st.lines.indexOf('\x1e', start);
        if (end < 0) end = st.lines.length();
        drawBox(st.lines.substring(start, end), 4, y, 192, y + slot - 8, 2);
        start = end + 1;
        y += slot;
      }
      continue;
    }


    display.setFont(&FreeSansBold12pt7b);
    if (st.total <= 0) {
      // Untimed: task only, centred in the body
      drawBox(st.body, 4, BODY_TOP, 192, BODY_BOTTOM, 5, true);
      continue;
    }

    // Timed: body (up to 4 lines), small timer just above a thin progress bar
    drawBox(st.body, 4, BODY_TOP, 192, 136, 4);

    bool over = rem <= 0;
    if (over) display.fillRect(0, 139, 200, 22, GxEPD_BLACK);
    display.setTextColor(over ? GxEPD_WHITE : GxEPD_BLACK);
    display.setFont(&FreeSansBold12pt7b);
    drawCentered(timerText(rem), 157);  // always one short line ("18 min", "+3 min")
    display.setTextColor(GxEPD_BLACK);

    display.drawRect(4, 165, 192, 8, GxEPD_BLACK);
    long done = st.total - (rem < 0 ? 0 : rem);
    int fill = (int)(188L * done / st.total);
    if (fill > 188) fill = 188;
    display.fillRect(6, 167, fill, 4, GxEPD_BLACK);
  } while (display.nextPage());
  display.powerOff();  // panel keeps the image without power
}

// ---- BLE -------------------------------------------------------------------
const char *resetReasonName() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_SW: return "restart";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_INT_WDT: case ESP_RST_TASK_WDT: case ESP_RST_WDT: return "watchdog";
    case ESP_RST_DEEPSLEEP: return "wake from sleep";
    case ESP_RST_BROWNOUT: return "brownout (low battery)";
    case ESP_RST_USB: return "usb";
    default: return "other";
  }
}

void setupBle() {
  BLE.begin("FocusInk");
  BLEServer server = BLE.createServer();
  server.onConnect([](BLEServer &, const BLEConnInfo &) { connected = true; });
  server.onDisconnect([](BLEServer &, const BLEConnInfo &, uint8_t) {
    connected = subscribed = false;
    fastAdvSince = millis();
  });
  server.advertiseOnDisconnect(true);

  BLEService svc = server.createService(BLEUUID(SERVICE_UUID));
  BLECharacteristic stateChar = svc.createCharacteristic(
      BLEUUID(STATE_UUID), BLEProperty::Write | BLEProperty::WriteNR, BLEPermission::WriteOpen);
  stateChar.onWrite([](BLECharacteristic &c, const BLEConnInfo &) {
    String v = c.getStringValue();
    portENTER_CRITICAL(&mux);
    pending = v;
    hasPending = true;
    portEXIT_CRITICAL(&mux);
  });
  eventChar = svc.createCharacteristic(BLEUUID(EVENT_UUID), BLEProperty::Read | BLEProperty::Notify, BLEPermission::ReadOpen);
  eventChar.onSubscribe([](BLECharacteristic &, const BLEConnInfo &, uint16_t v) { subscribed = v != 0; });
  // The Mac reads this once after connecting: why the board last (re)started.
  eventChar.setValue(String("reset:") + resetReasonName());
  BTStatus status = server.start();
  Serial.printf("server start: %s\n", status.toString());

  BLE.getAdvertising().addServiceUUID(BLEUUID(SERVICE_UUID));
  BLE.getAdvertising().setScanResponse(true);
  BLE.startAdvertising();
}

void queueEvent(const char *ev) {
  portENTER_CRITICAL(&qmux);
  if (queued == QUEUE_LEN) {  // drop the oldest
    for (int i = 1; i < QUEUE_LEN; i++) { queuedEv[i - 1] = queuedEv[i]; queuedAt[i - 1] = queuedAt[i]; }
    queued--;
  }
  queuedEv[queued] = ev; queuedAt[queued] = millis(); queued++;
  portEXIT_CRITICAL(&qmux);
}

void sendEvent(const char *ev) {
  lastActivity = millis();
  Serial.printf("button: %s (connected=%d, subscribed=%d)\n", ev, (int)connected, (int)subscribed);
  if (connected && subscribed && !queued) { eventChar.notify((const uint8_t *)ev, strlen(ev)); return; }
  queueEvent(ev);
  fastAdvSince = millis();  // be easy to find while the press waits
}

// Deliver queued presses once the Mac listens again; forget ones older than QUEUE_MAX_MS.
void flushQueue() {
  while (queued) {
    portENTER_CRITICAL(&qmux);
    const char *ev = queuedEv[0]; uint32_t at = queuedAt[0];
    bool fresh = millis() - at < QUEUE_MAX_MS;
    if (fresh && !(connected && subscribed)) { portEXIT_CRITICAL(&qmux); return; }
    for (int i = 1; i < queued; i++) { queuedEv[i - 1] = queuedEv[i]; queuedAt[i - 1] = queuedAt[i]; }
    queued--;
    portEXIT_CRITICAL(&qmux);
    if (fresh) {
      Serial.printf("queued: %s\n", ev);
      eventChar.notify((const uint8_t *)ev, strlen(ev));
      delay(80);
    }
  }
}

// ---- buttons ---------------------------------------------------------------
// Short press reported on release (after a 350 ms double-press window if doubleEv is set);
// long press (if longEv) once after 1 s.
struct Button {
  int pin; const char *shortEv; const char *longEv; const char *doubleEv;
  bool down = false, longSent = false, waiting = false, second = false;
  uint32_t since = 0, releasedAt = 0;
  void poll() {
    bool pressed = digitalRead(pin) == LOW;
    uint32_t now = millis();
    if (pressed && !down) {
      down = true; longSent = false; since = now;
      if (waiting && doubleEv) { second = true; waiting = false; }
    } else if (pressed && down && longEv && !longSent && !second && now - since > 1000) {
      longSent = true; sendEvent(longEv);
    } else if (!pressed && down) {
      down = false;
      if (second) { second = false; sendEvent(doubleEv); }
      else if (!longSent && now - since > 30) {
        if (doubleEv) { waiting = true; releasedAt = now; } else sendEvent(shortEv);
      }
    }
    if (waiting && !down && now - releasedAt > 350) { waiting = false; sendEvent(shortEv); }
  }
};
Button bootBtn{BOOT_BTN, "boot", "boot_long", nullptr};
Button pwrBtn{PWR_BTN, "pwr", nullptr, "pwr_double"};

// Both buttons held 3 s = restart. The restart happens on release, so BOOT (GPIO0, a strapping
// pin) isn't held low while the chip comes back up. No single-button events are sent meanwhile.
const uint32_t COMBO_MS = 3000;
volatile bool restartRequested = false;

// Poll buttons in their own task so presses aren't lost during a 2 s e-paper refresh.
void buttonTask(void *) {
  uint32_t bothSince = 0;
  bool armed = false;
  for (;;) {
    bool boot = digitalRead(BOOT_BTN) == LOW, pwr = digitalRead(PWR_BTN) == LOW;
    if (boot && pwr) {
      if (!bothSince) bothSince = millis();
      // swallow whatever each button was about to report
      bootBtn.longSent = true; bootBtn.waiting = false;
      pwrBtn.longSent = true; pwrBtn.waiting = false; pwrBtn.second = false;
      if (!armed && millis() - bothSince > COMBO_MS) { armed = true; Serial.println("restart armed"); }
    } else {
      bothSince = 0;
      if (armed && !boot && !pwr) restartRequested = true;
    }
    if (!armed) { bootBtn.poll(); pwrBtn.poll(); }
    else { bootBtn.down = boot; pwrBtn.down = pwr; }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

// ---- deep sleep --------------------------------------------------------------
void goToSleep() {
  Serial.println("deep sleep");
  if (connected && subscribed) {  // tell the Mac, so it shows "asleep" instead of a lost link
    eventChar.notify((const uint8_t *)"sleep", 5);
    delay(150);
  }
  render(false, "Asleep - press a button");
  display.hibernate();
  digitalWrite(EPD_PWR, HIGH);
  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
  esp_sleep_enable_ext1_wakeup_io((1ULL << BOOT_BTN) | (1ULL << PWR_BTN), ESP_EXT1_WAKEUP_ANY_LOW);
  rtc_gpio_pullup_en((gpio_num_t)BOOT_BTN);
  rtc_gpio_pullup_en((gpio_num_t)PWR_BTN);
  rtc_gpio_hold_en((gpio_num_t)VBAT_PWR);  // keep the battery switch on while asleep
  esp_deep_sleep_start();
}

void maybeSleep() {
  if (!onBattery()) return;
  uint32_t now = millis();
  bool counting = st.valid && (st.mode == "run" || st.mode == "up");
  bool idleTooLong = !counting && now - lastActivity > SLEEP_IDLE_MIN * 60000UL;
  bool goneTooLong = !connected && now - lastConnected > SLEEP_DISCONNECTED_MIN * 60000UL;
  if (idleTooLong || goneTooLong) goToSleep();
}

// ---- main ------------------------------------------------------------------
void setup() {
  setCpuFrequencyMhz(80);  // lowest speed BLE supports
  Serial.begin(115200);
  rtc_gpio_hold_dis((gpio_num_t)VBAT_PWR);
  pinMode(VBAT_PWR, OUTPUT); digitalWrite(VBAT_PWR, HIGH);
  pinMode(AUDIO_PWR, OUTPUT); digitalWrite(AUDIO_PWR, HIGH);
  pinMode(EPD_PWR, OUTPUT); digitalWrite(EPD_PWR, LOW);
  if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT1) {
    rtc_gpio_deinit((gpio_num_t)BOOT_BTN);
    rtc_gpio_deinit((gpio_num_t)PWR_BTN);
  }
  pinMode(BOOT_BTN, INPUT_PULLUP);
  pinMode(PWR_BTN, INPUT_PULLUP);
  analogReadResolution(12);
  analogSetPinAttenuation(BAT_ADC, ADC_11db);
  delay(50);
  readBattery();

  SPI.begin(EPD_SCK, -1, EPD_MOSI, EPD_CS);
  display.init(115200, true, 10, false);
  display.setRotation(0);

  setupBle();
  fastAdvSince = millis();
  if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT1) {
    // The press that woke the board counts: queue it for the Mac. If the button is still held,
    // make its release a no-op so it isn't counted twice.
    uint64_t woke = esp_sleep_get_ext1_wakeup_status();
    Button &b = (woke & (1ULL << PWR_BTN)) ? pwrBtn : bootBtn;
    queueEvent(b.shortEv);
    if (digitalRead(b.pin) == LOW) { b.down = true; b.longSent = true; }
  }
  xTaskCreatePinnedToCore(buttonTask, "buttons", 4096, nullptr, 2, nullptr, 0);
  lastActivity = lastConnected = millis();
  render(true);
  lastFullAt = millis();

  // If the main loop ever hangs for 30 s (a full refresh takes ~2 s), the watchdog reboots the board.
  esp_task_wdt_config_t wdt = {.timeout_ms = 30000, .idle_core_mask = 0, .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt);
  enableLoopWDT();
}

// Bluetooth should advertise again after a disconnect; if it ever stops, restart it.
void keepAdvertising() {
  static uint32_t lastCheck = 0;
  static int speed = -1;  // 1 fast, 0 slow
  if (connected) { speed = -1; return; }
  bool fast = millis() - fastAdvSince < FAST_ADV_MS;
  if ((int)fast != speed) {  // switch interval: stop, set, start
    BLEAdvertising adv = BLE.getAdvertising();
    adv.stop();
    if (fast) adv.setInterval(30, 60); else adv.setInterval(800, 1000);
    BTStatus st = BLE.startAdvertising();
    Serial.printf("advertising %s: %s\n", fast ? "fast" : "slow", st.toString());
    speed = fast;
    lastCheck = millis();
    return;
  }
  if (millis() - lastCheck < 5000) return;
  lastCheck = millis();
  if (!BLE.getAdvertising().isAdvertising()) {
    BTStatus st = BLE.startAdvertising();
    Serial.printf("advertising restarted: %s\n", st.toString());
  }
}

void loop() {
  if (restartRequested) {
    render(true, "Restarting...");
    delay(200);
    esp_restart();
  }
  keepAdvertising();
  flushQueue();
  if (hasPending) {
    String p;
    portENTER_CRITICAL(&mux);
    p = pending;
    hasPending = false;
    portEXIT_CRITICAL(&mux);
    // A periodic resync of the same screen is not "activity"; anything else is.
    if (p != lastPayload) lastActivity = millis();
    lastPayload = p;
    parsePayload(p);
  }
  if (connected) lastConnected = millis();
  if (millis() - lastBatteryRead > 30000) readBattery();

  // Redraw only when something visible changes (minute tick, text, link state, progress step, battery step)
  long rem = remainingNow();
  int step = st.total > 0 ? (int)(188L * (st.total - max(rem, 0L)) / st.total) / 8 : 0;
  int bat = onBattery() ? batteryPct() / 10 : -1;
  // Partial refreshes for everything; an occasional full refresh clears ghosting (see FULL_EVERY).
  String layoutKey = st.header + "|" + st.body + "|" + st.lines + "|" + st.right + "|" + st.footer + "|" + st.ints;
  // While a press waits for the Mac, say so instead of looking frozen.
  String footerOv = (!connected && queued) ? "Connecting..." : "";
  String drawKey = layoutKey + "|" + st.right + "|" + st.footer + "|" + timerText(rem) + "|" + st.ints + "|" +
                   connected + "|" + step + "|" + bat + "|" + footerOv;
  if (drawKey != pendingDrawKey) { pendingDrawKey = drawKey; drawKeyChangedAt = millis(); }
  if (drawKey != lastDrawKey && millis() - drawKeyChangedAt >= SETTLE_MS) {
    bool textChanged = layoutKey != lastLayoutKey;
    if (textChanged) partialCount++;
    bool full = textChanged && partialCount >= FULL_EVERY && millis() - lastFullAt >= FULL_MIN_GAP_MS;
    render(full, footerOv);
    if (full) { partialCount = 0; lastFullAt = millis(); }
    lastLayoutKey = layoutKey;
    lastDrawKey = drawKey;
  }
  maybeSleep();
  delay(50);
}
