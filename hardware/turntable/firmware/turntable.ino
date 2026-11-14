/*
 * Ferret turntable firmware  (ESP32 + NEMA17 via TMC2209 or A4988)
 * --------------------------------------------------------------------------
 * Drives a 200 mm scan turntable for the Ferret 3D-scanning pipeline.
 *
 * Serial protocol (115200 baud, one command per newline-terminated line):
 *     R<degrees>   rotate by a signed angle, e.g. "R90", "R-45.5"
 *     S<rpm>       set rotation speed in RPM,  e.g. "S5"
 *     H            home: zero the logical angle
 *     P            print current logical angle
 * Every command is acknowledged with "OK ...\n", or "ERR ...\n" on failure.
 * A rotation blocks (stays quiet) until the move completes, then acks.
 *
 * Optional MQTT mirror (Bambu-Lab style topic layout, see CONFIG below):
 *     subscribe  ferret/turntable/rotate   payload = degrees (e.g. "30")
 *     subscribe  ferret/turntable/speed    payload = rpm
 *     publish    ferret/turntable/status   retained JSON after each move
 *
 * Wiring and flashing: see ../README.md.
 * NOTE: not flashed/verified on hardware in this session (no board attached).
 */

// ======================== CONFIG ==========================================
// --- Stepper geometry ---
static const int   STEPS_PER_REV   = 200;   // NEMA17 1.8 deg/step
static const int   MICROSTEPS      = 16;    // driver microstep setting
// Optional reduction between motor shaft and platter (1.0 = direct drive).
static const float GEAR_RATIO      = 1.0f;
static const float DEFAULT_RPM     = 5.0f;
static const float MIN_RPM         = 0.1f;
static const float MAX_RPM         = 60.0f;

// --- Driver pins (TMC2209 in STEP/DIR mode, or A4888/A4988) ---
static const int   PIN_STEP        = 26;
static const int   PIN_DIR         = 25;
static const int   PIN_EN          = 27;    // active-LOW enable on most drivers
static const bool  INVERT_DIR      = false; // flip if CCW/CW is reversed

// --- MQTT (set ENABLE_MQTT to 1 to compile in WiFi + PubSubClient) ---
#define ENABLE_MQTT 0
#if ENABLE_MQTT
  static const char* WIFI_SSID   = "your-ssid";
  static const char* WIFI_PASS   = "your-pass";
  static const char* MQTT_HOST   = "your-mqtt-host";
  static const int   MQTT_PORT   = 1883;
  static const char* MQTT_USER   = "";      // optional
  static const char* MQTT_PASS   = "";      // optional
  static const char* TOPIC_ROTATE = "ferret/turntable/rotate";
  static const char* TOPIC_SPEED  = "ferret/turntable/speed";
  static const char* TOPIC_STATUS = "ferret/turntable/status";
  static const char* MQTT_CLIENT_ID = "ferret-turntable";
#endif
// ==========================================================================

// Derived constants.
static const long  STEPS_PER_PLATTER_REV =
    (long)(STEPS_PER_REV * MICROSTEPS * GEAR_RATIO);
static const float STEPS_PER_DEGREE =
    (float)STEPS_PER_PLATTER_REV / 360.0f;

// Runtime state.
static float currentAngle = 0.0f;   // logical platter angle, degrees
static float currentRpm   = DEFAULT_RPM;

#if ENABLE_MQTT
  #include <WiFi.h>
  #include <PubSubClient.h>
  WiFiClient   wifiClient;
  PubSubClient mqtt(wifiClient);
#endif

// --- Low-level motion -----------------------------------------------------

// Half-period (microseconds) of the STEP pulse for the active speed. One full
// step is two toggles, so step_interval = 60e6 / (steps_per_rev_eff * rpm) / 2.
static unsigned long stepHalfPeriodUs() {
  float stepsPerSec = (currentRpm / 60.0f) * (float)STEPS_PER_PLATTER_REV;
  if (stepsPerSec < 1.0f) stepsPerSec = 1.0f;
  unsigned long fullPeriod = (unsigned long)(1000000.0f / stepsPerSec);
  unsigned long half = fullPeriod / 2;
  if (half < 2) half = 2;   // respect driver minimum pulse width
  return half;
}

// Blocking rotation by a signed angle. Returns steps actually issued.
static long rotateDegrees(float degrees) {
  bool forward = (degrees >= 0.0f);
  long steps = (long)lround(fabs(degrees) * STEPS_PER_DEGREE);
  if (steps == 0) return 0;

  bool dirLevel = forward ^ INVERT_DIR;
  digitalWrite(PIN_DIR, dirLevel ? HIGH : LOW);
  digitalWrite(PIN_EN, LOW);          // enable driver (active-LOW)
  delayMicroseconds(5);               // DIR setup time

  unsigned long half = stepHalfPeriodUs();
  for (long i = 0; i < steps; i++) {
    digitalWrite(PIN_STEP, HIGH);
    delayMicroseconds(half);
    digitalWrite(PIN_STEP, LOW);
    delayMicroseconds(half);
  }

  // Update logical angle from the steps we truly issued (avoids drift).
  float movedDeg = (float)steps / STEPS_PER_DEGREE;
  currentAngle += forward ? movedDeg : -movedDeg;
  currentAngle = fmodf(currentAngle, 360.0f);
  if (currentAngle < 0.0f) currentAngle += 360.0f;

  // Leave the driver energized to hold position during the scan capture.
  return steps;
}

static bool setSpeed(float rpm) {
  if (rpm < MIN_RPM || rpm > MAX_RPM) return false;
  currentRpm = rpm;
  return true;
}

// --- Command handling -----------------------------------------------------

static void ackf(const char* fmt, ...) {
  char buf[96];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  Serial.println(buf);
}

#if ENABLE_MQTT
static void publishStatus() {
  char payload[96];
  snprintf(payload, sizeof(payload),
           "{\"angle\":%.2f,\"rpm\":%.2f}", currentAngle, currentRpm);
  mqtt.publish(TOPIC_STATUS, payload, true /* retained */);
}
#endif

// Parse and execute one command line. cmd is a NUL-terminated, trimmed string.
static void handleCommand(const char* cmd) {
  if (cmd[0] == '\0') return;

  switch (cmd[0]) {
    case 'R': case 'r': {
      char* end = nullptr;
      float deg = strtof(cmd + 1, &end);
      if (end == cmd + 1) { ackf("ERR bad angle: %s", cmd); return; }
      rotateDegrees(deg);
      ackf("OK R %.2f angle=%.2f", deg, currentAngle);
      break;
    }
    case 'S': case 's': {
      char* end = nullptr;
      float rpm = strtof(cmd + 1, &end);
      if (end == cmd + 1) { ackf("ERR bad rpm: %s", cmd); return; }
      if (!setSpeed(rpm)) { ackf("ERR rpm out of range: %.2f", rpm); return; }
      ackf("OK S %.2f", currentRpm);
      break;
    }
    case 'H': case 'h':
      currentAngle = 0.0f;
      ackf("OK H angle=0.00");
      break;
    case 'P': case 'p':
      ackf("OK %.2f", currentAngle);
      break;
    default:
      ackf("ERR unknown command: %s", cmd);
      break;
  }
}

// --- Serial line reader ---------------------------------------------------

static char    lineBuf[64];
static uint8_t lineLen = 0;

static void pollSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      lineBuf[lineLen] = '\0';
      handleCommand(lineBuf);
      lineLen = 0;
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    } else {
      // Overflow: reset the buffer and report.
      lineLen = 0;
      ackf("ERR line too long");
    }
  }
}

// --- MQTT plumbing --------------------------------------------------------

#if ENABLE_MQTT
static void onMqttMessage(char* topic, byte* payload, unsigned int len) {
  char buf[32];
  unsigned int n = (len < sizeof(buf) - 1) ? len : sizeof(buf) - 1;
  memcpy(buf, payload, n);
  buf[n] = '\0';

  if (strcmp(topic, TOPIC_ROTATE) == 0) {
    rotateDegrees(strtof(buf, nullptr));
    publishStatus();
  } else if (strcmp(topic, TOPIC_SPEED) == 0) {
    setSpeed(strtof(buf, nullptr));
    publishStatus();
  }
}

static void mqttConnect() {
  while (!mqtt.connected()) {
    bool ok = (strlen(MQTT_USER) > 0)
                ? mqtt.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASS)
                : mqtt.connect(MQTT_CLIENT_ID);
    if (ok) {
      mqtt.subscribe(TOPIC_ROTATE);
      mqtt.subscribe(TOPIC_SPEED);
      publishStatus();
      return;
    }
    delay(1000);
  }
}

static void wifiConnect() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(250);
  }
}
#endif

// --- Arduino entry points -------------------------------------------------

void setup() {
  Serial.begin(115200);
  pinMode(PIN_STEP, OUTPUT);
  pinMode(PIN_DIR, OUTPUT);
  pinMode(PIN_EN, OUTPUT);
  digitalWrite(PIN_STEP, LOW);
  digitalWrite(PIN_DIR, LOW);
  digitalWrite(PIN_EN, HIGH);   // start disabled (active-LOW) to stay cool

#if ENABLE_MQTT
  wifiConnect();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  mqttConnect();
#endif

  Serial.println("OK ready ferret-turntable");
}

void loop() {
  pollSerial();
#if ENABLE_MQTT
  if (!mqtt.connected()) mqttConnect();
  mqtt.loop();
#endif
}
