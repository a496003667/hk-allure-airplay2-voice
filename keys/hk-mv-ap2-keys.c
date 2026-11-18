#define _GNU_SOURCE

/*
 * LD_PRELOAD hook for the stock mv_ioguard: while an AirPlay 2 sender is
 * connected, the physical volume and play/pause keys control that sender
 * instead of the stock player.
 *
 * mv_ioguard turns MCU key events into SocketClientReadWriteMsg() calls to
 * /tmp/Requesta01controller.  Play/pause arrives as "setPlayerCmd:onepause";
 * the physical volume keys arrive as an absolute level computed by the MCU,
 * "setPlayerCmd:ext_slave_vol:<0..100>".  The hook tracks the level the MCU
 * last showed (from those messages and from the GNOTIFY=VOLUMECHANGE<n>
 * display updates mv_ioguard receives) and turns a higher or lower value
 * into one volume step.  "vol++"/"vol--" (stock key-event socket) map
 * directly.
 * When /tmp/hk-ap2/run/buttons.forward exists (the lights bridge creates it
 * while a sender holds an RTSP connection) those three commands are published
 * to the local MQTT broker as Shairport Sync 5.6 remote commands
 * ("hk-ap2/remote": volumeup, volumedown, playpause) and not passed to the
 * stock player.  If the flag is absent or /tmp/hk-keys/disable exists, the
 * original call runs unchanged.  A failed publish falls back to the stock
 * player for play/pause, but key volume levels are dropped (see below).
 *
 * Diagnostics: while /tmp/hk-keys/trace exists, every outgoing IPC message
 * (SocketClientReadWriteMsg and NotifyMessage) is appended to keys.log.
 *
 * Voice events: the stock assistant (asr_tts) drives the ring with
 * GNOTIFY=MCULED* messages (LISTENING after its local wake word, THINKING,
 * PLAYTTS, RECOERROR, IDLE, ...).  Each one mv_ioguard receives is appended
 * to /tmp/hk-keys/voice.events as "<uptime-ms> <message>" (bounded), so a
 * local voice assistant can use the stock wake word without touching asr_tts.
 */

#include <arpa/inet.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

typedef int (*rw_msg_fn)(const char *path, const char *message, int length,
                         char *response, int *response_length,
                         int response_capacity);
typedef int (*notify_fn)(int target, const char *message);

typedef int (*socket_read_fn)(void *server, char *buffer, int length);

static rw_msg_fn real_rw_msg;
static notify_fn real_notify;
static socket_read_fn real_socket_read;

/* Volume level the MCU currently displays, or -1 until first seen. */
static volatile int mcu_volume = -1;

static const char player_socket[] = "/tmp/Requesta01controller";
static const char forward_flag[] = "/tmp/hk-ap2/run/buttons.forward";
static const char disable_flag[] = "/vendor/hk-keys/disable";
static const char legacy_disable_flag[] = "/tmp/hk-keys/disable";
static const char key_log[] = "/tmp/hk-keys/keys.log";
static const char trace_flag[] = "/tmp/hk-keys/trace";
static const char voice_events[] = "/tmp/hk-keys/voice.events";
static const char remote_topic[] = "hk-ap2/remote";

static void clear_preload_environment(void) __attribute__((constructor));

static void clear_preload_environment(void) {
  /* Keep the preload out of the stock helpers mv_ioguard launches. */
  unsetenv("LD_PRELOAD");
}

static int message_is(const char *message, int length, const char *value) {
  int value_length = (int)strlen(value);
  return length >= value_length &&
         memcmp(message, value, (size_t)value_length) == 0 &&
         (length == value_length || message[value_length] == '\0');
}

static int parse_level(const char *message, int length, const char *prefix) {
  int prefix_length = (int)strlen(prefix);
  int value = 0;
  int digits = 0;
  if (length < prefix_length || memcmp(message, prefix, (size_t)prefix_length) != 0) {
    return -1;
  }
  for (int i = prefix_length; i < length && message[i] >= '0' && message[i] <= '9'; i++) {
    value = value * 10 + (message[i] - '0');
    if (++digits > 3) {
      return -1;
    }
  }
  return digits > 0 && value <= 100 ? value : -1;
}

static const char *remote_command_for(const char *message, int length) {
  int level = parse_level(message, length, "setPlayerCmd:ext_slave_vol:");
  if (level >= 0) {
    int previous = mcu_volume;
    mcu_volume = level;
    if (previous < 0 || level == previous) {
      return NULL;
    }
    return level > previous ? "volumeup" : "volumedown";
  }
  if (message_is(message, length, "setPlayerCmd:vol++")) {
    return "volumeup";
  }
  if (message_is(message, length, "setPlayerCmd:vol--")) {
    return "volumedown";
  }
  if (message_is(message, length, "setPlayerCmd:onepause")) {
    return "playpause";
  }
  return NULL;
}

static void log_line(const char *action, const char *result) {
  int fd = open(key_log, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
  if (fd < 0) {
    return;
  }
  /* Bounded: start over once the log passes 32 KiB. */
  if (lseek(fd, 0, SEEK_END) > 32768) {
    if (ftruncate(fd, 0) != 0) {
      close(fd);
      return;
    }
  }
  char line[256];
  int used = snprintf(line, sizeof(line), "%ld %s %.200s\n", (long)time(NULL),
                      action, result);
  if (used > 0 && used < (int)sizeof(line)) {
    (void)write(fd, line, (size_t)used);
  }
  close(fd);
}

static int send_all(int fd, const unsigned char *data, size_t length) {
  while (length > 0) {
    ssize_t written = write(fd, data, length);
    if (written <= 0) {
      return -1;
    }
    data += written;
    length -= (size_t)written;
  }
  return 0;
}

static size_t put_remaining_length(unsigned char *out, size_t length) {
  size_t used = 0;
  do {
    unsigned char byte = (unsigned char)(length % 128);
    length /= 128;
    if (length > 0) {
      byte |= 0x80;
    }
    out[used++] = byte;
  } while (length > 0);
  return used;
}

/* MQTT 3.1.1 CONNECT + QoS 0 PUBLISH + DISCONNECT to 127.0.0.1:1883. */
static int mqtt_publish(const char *topic, const char *payload) {
  static const unsigned char connect_body[] = {
      0x00, 0x04, 'M', 'Q', 'T', 'T', 0x04, 0x02, 0x00, 0x0a,
      0x00, 0x0b, 'h', 'k', '-', 'm', 'v', '-', 'k', 'e', 'y', 's', '0'};
  unsigned char packet[256];
  unsigned char reply[4];
  size_t topic_length = strlen(topic);
  size_t payload_length = strlen(payload);
  size_t body_length = 2 + topic_length + payload_length;
  size_t used;
  struct sockaddr_in address;
  struct timeval timeout = {1, 0};
  int fd;

  if (body_length + 8 > sizeof(packet)) {
    return -1;
  }
  fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (fd < 0) {
    return -1;
  }
  setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
  memset(&address, 0, sizeof(address));
  address.sin_family = AF_INET;
  address.sin_port = htons(1883);
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  if (connect(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
    close(fd);
    return -1;
  }

  packet[0] = 0x10;
  used = 1 + put_remaining_length(packet + 1, sizeof(connect_body));
  memcpy(packet + used, connect_body, sizeof(connect_body));
  used += sizeof(connect_body);
  if (send_all(fd, packet, used) != 0 || read(fd, reply, 4) != 4 ||
      reply[0] != 0x20 || reply[3] != 0) {
    close(fd);
    return -1;
  }

  packet[0] = 0x30;
  used = 1 + put_remaining_length(packet + 1, body_length);
  packet[used++] = (unsigned char)(topic_length >> 8);
  packet[used++] = (unsigned char)topic_length;
  memcpy(packet + used, topic, topic_length);
  used += topic_length;
  memcpy(packet + used, payload, payload_length);
  used += payload_length;
  packet[used++] = 0xe0; /* DISCONNECT */
  packet[used++] = 0x00;
  int result = send_all(fd, packet, used);
  close(fd);
  return result;
}

int SocketClientReadWriteMsg(const char *path, const char *message, int length,
                             char *response, int *response_length,
                             int response_capacity) {
  if (real_rw_msg == NULL) {
    real_rw_msg = (rw_msg_fn)dlsym(RTLD_NEXT, "SocketClientReadWriteMsg");
    if (real_rw_msg == NULL) {
      return -1;
    }
  }

  if (access(trace_flag, F_OK) == 0) {
    char detail[224];
    snprintf(detail, sizeof(detail), "%.40s %.*s", path != NULL ? path : "-",
             message != NULL && length > 0 ? (length > 160 ? 160 : length) : 0,
             message != NULL ? message : "");
    log_line("ipc", detail);
  }

  const char *command = NULL;
  int is_key_volume = 0;
  if (path != NULL && message != NULL && length > 0 &&
      strcmp(path, player_socket) == 0) {
    is_key_volume = parse_level(message, length, "setPlayerCmd:ext_slave_vol:") >= 0;
    command = remote_command_for(message, length);
  }
  int forwarding = (command != NULL || is_key_volume) &&
                   access(forward_flag, F_OK) == 0 &&
                   access(disable_flag, F_OK) != 0 && access(legacy_disable_flag, F_OK) != 0;
  if (forwarding) {
    int sent = command == NULL || mqtt_publish(remote_topic, command) == 0;
    /* One physical key press moves the stock volume ~12%; one AirPlay step is
     * 1.875 dB (6.25% of the slider), so send two to keep the same feel. */
    if (sent && command != NULL && is_key_volume) {
      sent = mqtt_publish(remote_topic, command) == 0;
    }
    /* A key volume level must never reach the stock player while a sender
     * owns the speaker, even without a direction (first press) or when the
     * publish fails: it would move the stock volume past the AP2 safety cap. */
    if (sent || is_key_volume) {
      log_line(command != NULL ? command : "volume-level",
               sent ? "forwarded" : "publish failed; dropped");
      if (response != NULL && response_capacity > 0) {
        response[0] = '\0';
      }
      if (response_length != NULL) {
        *response_length = 0;
      }
      return length;
    }
    log_line(command, "publish failed; stock");
  }
  return real_rw_msg(path, message, length, response, response_length,
                     response_capacity);
}

int NotifyMessage(int target, const char *message) {
  if (real_notify == NULL) {
    real_notify = (notify_fn)dlsym(RTLD_NEXT, "NotifyMessage");
    if (real_notify == NULL) {
      return -1;
    }
  }
  if (access(trace_flag, F_OK) == 0) {
    char detail[200];
    snprintf(detail, sizeof(detail), "%d %.160s", target,
             message != NULL ? message : "");
    log_line("notify", detail);
  }
  return real_notify(target, message);
}

static void record_voice_event(const char *message, int length) {
  static const char prefix[] = "GNOTIFY=MCULED";
  if (length < (int)sizeof(prefix) - 1 ||
      memcmp(message, prefix, sizeof(prefix) - 1) != 0) {
    return;
  }
  int fd = open(voice_events, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
  if (fd < 0) {
    return;
  }
  if (lseek(fd, 0, SEEK_END) > 16384 && ftruncate(fd, 0) != 0) {
    close(fd);
    return;
  }
  struct timespec now;
  clock_gettime(CLOCK_MONOTONIC, &now);
  int text = 0;
  while (text < length && text < 96 && message[text] != '\0' && message[text] != '\n') {
    text++;
  }
  char line[160];
  int used = snprintf(line, sizeof(line), "%ld %.*s\n",
                      (long)now.tv_sec * 1000 + now.tv_nsec / 1000000, text, message);
  if (used > 0 && used < (int)sizeof(line)) {
    (void)write(fd, line, (size_t)used);
  }
  close(fd);
}

int UnixSocketServer_read(void *server, char *buffer, int length) {
  if (real_socket_read == NULL) {
    real_socket_read = (socket_read_fn)dlsym(RTLD_NEXT, "UnixSocketServer_read");
    if (real_socket_read == NULL) {
      return -1;
    }
  }
  int result = real_socket_read(server, buffer, length);
  if (result > 0) {
    int level = parse_level(buffer, result, "GNOTIFY=VOLUMECHANGE");
    if (level >= 0) {
      mcu_volume = level;
    }
    record_voice_event(buffer, result);
  }
  return result;
}
