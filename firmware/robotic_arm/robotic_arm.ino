/*
 * RoboArm - Arduino Uno firmware for a 6-axis hobby-servo robotic arm.
 *
 * Talks a line-based ASCII protocol over USB serial at 115200 baud.
 * Motion is interpolated in the main loop (non-blocking) so the arm
 * eases toward its target instead of snapping to it.
 *
 * Wiring
 *   Servo signal : D3 D5 D6 D9 D10 D11  (base, shoulder, elbow, wristPitch, wristRoll, gripper)
 *   Servo power  : separate 5-6V supply, >=2A. Tie its GND to Arduino GND.
 *   Teach pots   : A0..A5 (optional, only needed for TEACH mode)
 *
 * Commands (one per line, newline terminated, case-insensitive)
 *   PING              -> OK PONG <name> <version> <joints>
 *   G                 -> POS a0 a1 a2 a3 a4 a5
 *   M a0 a1 ... a5    move all joints. "-" keeps a joint where it is.
 *   S <i> <angle>     move one joint
 *   J <i> <delta>     jog one joint by delta degrees
 *   SPD <deg/s>       max joint speed, 0..600 (0 = instant)
 *   HOME              go to the home pose
 *   ATT / DET         attach / detach (relax) all servos
 *   LIM <i> <lo> <hi> set travel limits for a joint
 *   LIMS              -> LIM i lo hi   (one line per joint)
 *   SAVE / LOAD       persist / restore limits + speed in EEPROM
 *   TEACH 0|1         teach mode: servos relax, pots on A0..A5 drive POS output
 *   STREAM <ms>       push a POS line every <ms> ms (0 = off)
 *
 * Replies are prefixed: OK / ERR / POS / LIM / RDY
 */

#include <Servo.h>
#include <EEPROM.h>

#define FW_NAME     "RoboArm"
#define FW_VERSION  "1.0"
#define NUM_JOINTS  6
#define EE_MAGIC    0x5A
#define EE_ADDR     0

const uint8_t SERVO_PIN[NUM_JOINTS] = { 3, 5, 6, 9, 10, 11 };
const uint8_t POT_PIN[NUM_JOINTS]   = { A0, A1, A2, A3, A4, A5 };
const int     HOME_POSE[NUM_JOINTS] = { 90, 90, 90, 90, 90, 20 };

Servo   servos[NUM_JOINTS];
float   current[NUM_JOINTS];        // live interpolated angle
int     target[NUM_JOINTS];         // where we are heading
uint8_t loLimit[NUM_JOINTS];
uint8_t hiLimit[NUM_JOINTS];
float   potFilt[NUM_JOINTS];        // smoothed pot readings (teach mode)

float         speedDps   = 120.0;   // max degrees per second, 0 = instant
bool          attached   = false;
bool          teachMode  = false;
unsigned int  streamMs   = 0;
unsigned long lastMotion = 0;
unsigned long lastStream = 0;

char    buf[64];
uint8_t blen = 0;

// ---------------------------------------------------------------- helpers

int clampJoint(uint8_t i, int a) {
  if (a < (int)loLimit[i]) a = loLimit[i];
  if (a > (int)hiLimit[i]) a = hiLimit[i];
  return a;
}

void attachAll() {
  if (attached) return;
  for (uint8_t i = 0; i < NUM_JOINTS; i++) {
    servos[i].attach(SERVO_PIN[i]);
    servos[i].write((int)current[i]);
  }
  attached = true;
}

void detachAll() {
  if (!attached) return;
  for (uint8_t i = 0; i < NUM_JOINTS; i++) servos[i].detach();
  attached = false;
}

void sendPos() {
  Serial.print(F("POS"));
  for (uint8_t i = 0; i < NUM_JOINTS; i++) {
    Serial.print(' ');
    Serial.print((int)(current[i] + 0.5));
  }
  Serial.println();
}

void saveEeprom() {
  int a = EE_ADDR;
  EEPROM.update(a++, EE_MAGIC);
  for (uint8_t i = 0; i < NUM_JOINTS; i++) {
    EEPROM.update(a++, loLimit[i]);
    EEPROM.update(a++, hiLimit[i]);
  }
  int spd = (int)speedDps;
  EEPROM.update(a++, spd & 0xFF);
  EEPROM.update(a++, (spd >> 8) & 0xFF);
}

bool loadEeprom() {
  int a = EE_ADDR;
  if (EEPROM.read(a++) != EE_MAGIC) return false;
  for (uint8_t i = 0; i < NUM_JOINTS; i++) {
    uint8_t lo = EEPROM.read(a++);
    uint8_t hi = EEPROM.read(a++);
    if (lo < hi && hi <= 180) { loLimit[i] = lo; hiLimit[i] = hi; }
  }
  int spd = EEPROM.read(a);
  spd |= (EEPROM.read(a + 1) << 8);
  if (spd >= 0 && spd <= 600) speedDps = spd;
  return true;
}

// ---------------------------------------------------------------- setup

void setup() {
  Serial.begin(115200);
  for (uint8_t i = 0; i < NUM_JOINTS; i++) {
    loLimit[i] = 0;
    hiLimit[i] = 180;
    current[i] = HOME_POSE[i];
    target[i]  = HOME_POSE[i];
    potFilt[i] = HOME_POSE[i];
  }
  loadEeprom();
  attachAll();
  lastMotion = millis();
  Serial.println(F("RDY " FW_NAME " " FW_VERSION));
}

// ---------------------------------------------------------------- motion

void updateMotion() {
  unsigned long now = millis();
  float dt = (now - lastMotion) / 1000.0;
  if (dt < 0.015) return;                 // ~66 Hz ceiling
  lastMotion = now;

  if (teachMode) {                        // pots drive the reported position
    for (uint8_t i = 0; i < NUM_JOINTS; i++) {
      float deg = analogRead(POT_PIN[i]) * (180.0 / 1023.0);
      potFilt[i] += (deg - potFilt[i]) * 0.25;   // EMA, kills ADC jitter
      current[i]  = clampJoint(i, (int)potFilt[i]);
      target[i]   = (int)current[i];
    }
    return;
  }

  if (!attached) return;
  float step = (speedDps <= 0) ? 1000.0 : speedDps * dt;
  for (uint8_t i = 0; i < NUM_JOINTS; i++) {
    float diff = target[i] - current[i];
    if (diff > step)       current[i] += step;
    else if (diff < -step) current[i] -= step;
    else                   current[i]  = target[i];
    servos[i].write((int)(current[i] + 0.5));
  }
}

// ---------------------------------------------------------------- parsing

char *nextTok(char **p) {
  while (**p == ' ' || **p == '\t') (*p)++;
  if (**p == 0) return NULL;
  char *start = *p;
  while (**p && **p != ' ' && **p != '\t') (*p)++;
  if (**p) { **p = 0; (*p)++; }
  return start;
}

void handleLine(char *line) {
  char *p   = line;
  char *cmd = nextTok(&p);
  if (!cmd) return;
  for (char *c = cmd; *c; c++) *c = toupper(*c);

  if (!strcmp(cmd, "PING") || !strcmp(cmd, "INFO")) {
    Serial.print(F("OK PONG " FW_NAME " " FW_VERSION " "));
    Serial.println(NUM_JOINTS);

  } else if (!strcmp(cmd, "G")) {
    sendPos();

  } else if (!strcmp(cmd, "M")) {
    if (teachMode) { Serial.println(F("ERR teach")); return; }
    attachAll();
    for (uint8_t i = 0; i < NUM_JOINTS; i++) {
      char *t = nextTok(&p);
      if (!t) break;
      if (t[0] == '-' && t[1] == 0) continue;      // "-" = leave this joint alone
      int v = atoi(t);
      if (v < 0) continue;
      target[i] = clampJoint(i, v);
    }
    Serial.println(F("OK"));

  } else if (!strcmp(cmd, "S") || !strcmp(cmd, "J")) {
    if (teachMode) { Serial.println(F("ERR teach")); return; }
    char *a = nextTok(&p);
    char *b = nextTok(&p);
    if (!a || !b) { Serial.println(F("ERR args")); return; }
    int i = atoi(a);
    if (i < 0 || i >= NUM_JOINTS) { Serial.println(F("ERR joint")); return; }
    attachAll();
    int v = (cmd[0] == 'S') ? atoi(b) : target[i] + atoi(b);
    target[i] = clampJoint(i, v);
    Serial.println(F("OK"));

  } else if (!strcmp(cmd, "SPD")) {
    char *a = nextTok(&p);
    if (!a) { Serial.println(F("ERR args")); return; }
    int v = atoi(a);
    if (v < 0 || v > 600) { Serial.println(F("ERR range")); return; }
    speedDps = v;
    Serial.println(F("OK"));

  } else if (!strcmp(cmd, "HOME")) {
    attachAll();
    for (uint8_t i = 0; i < NUM_JOINTS; i++) target[i] = clampJoint(i, HOME_POSE[i]);
    Serial.println(F("OK"));

  } else if (!strcmp(cmd, "ATT")) {
    attachAll();
    Serial.println(F("OK"));

  } else if (!strcmp(cmd, "DET")) {
    detachAll();
    Serial.println(F("OK"));

  } else if (!strcmp(cmd, "LIM")) {
    char *a = nextTok(&p), *b = nextTok(&p), *c = nextTok(&p);
    if (!a || !b || !c) { Serial.println(F("ERR args")); return; }
    int i = atoi(a), lo = atoi(b), hi = atoi(c);
    if (i < 0 || i >= NUM_JOINTS || lo < 0 || hi > 180 || lo >= hi) {
      Serial.println(F("ERR range"));
      return;
    }
    loLimit[i] = lo;
    hiLimit[i] = hi;
    target[i]  = clampJoint(i, target[i]);
    Serial.println(F("OK"));

  } else if (!strcmp(cmd, "LIMS")) {
    for (uint8_t i = 0; i < NUM_JOINTS; i++) {
      Serial.print(F("LIM "));
      Serial.print(i);
      Serial.print(' ');
      Serial.print(loLimit[i]);
      Serial.print(' ');
      Serial.println(hiLimit[i]);
    }

  } else if (!strcmp(cmd, "SAVE")) {
    saveEeprom();
    Serial.println(F("OK"));

  } else if (!strcmp(cmd, "LOAD")) {
    Serial.println(loadEeprom() ? F("OK") : F("ERR empty"));

  } else if (!strcmp(cmd, "TEACH")) {
    char *a = nextTok(&p);
    teachMode = (a && atoi(a) != 0);
    if (teachMode) {
      detachAll();                                   // let the arm be posed by hand
      for (uint8_t i = 0; i < NUM_JOINTS; i++)
        potFilt[i] = analogRead(POT_PIN[i]) * (180.0 / 1023.0);
    } else {
      for (uint8_t i = 0; i < NUM_JOINTS; i++) target[i] = (int)current[i];
      attachAll();
    }
    Serial.println(F("OK"));

  } else if (!strcmp(cmd, "STREAM")) {
    char *a = nextTok(&p);
    int v = a ? atoi(a) : 0;
    if (v <= 0)      streamMs = 0;
    else if (v < 20) streamMs = 20;
    else             streamMs = v;
    Serial.println(F("OK"));

  } else {
    Serial.println(F("ERR unknown"));
  }
}

// ---------------------------------------------------------------- loop

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (blen) {
        buf[blen] = 0;
        handleLine(buf);
        blen = 0;
      }
    } else if (blen < sizeof(buf) - 1) {
      buf[blen++] = c;
    }
  }

  updateMotion();

  if (streamMs && millis() - lastStream >= streamMs) {
    lastStream = millis();
    sendPos();
  }
}
