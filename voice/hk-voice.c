/*
 * hk-voice: on-device voice assistant client for the Harman Kardon Allure.
 *
 *   hk-voice run <in.wav> <out.raw>   ASR -> LLM -> TTS
 *   hk-voice ask <text>               LLM only, prints the reply
 *   hk-voice say <text> <out.raw>     TTS only
 *   hk-voice asr <in.wav>             ASR only, prints the text
 *
 * Input: the stock asr_tts recording (44.1 kHz, 16-bit, stereo; the louder
 * channel is the microphone).  Output: raw S16_LE 48 kHz stereo for
 * "aplay -D hw:0,0", already attenuated to follow the AirPlay volume.
 *
 * HTTPS goes through the stock libcurl/OpenSSL 1.1 (dlopen, so no headers or
 * import library are needed).  Keys are read from the config file and never
 * printed.  No libm: the toolchain's libm carries newer symbol versions than
 * the device's glibc 2.19.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern int malloc_trim(size_t);

#define CONF_PATH "/vendor/hk-voice/voice.conf"
#define TOKEN_PATH "/vendor/hk-voice/baidu.token"
#define HIST_PATH "/tmp/hk-voice/history"
#define VOLUME_LOG "/tmp/hk-ap2/run/volume.log"
#define CA_FILE (access("/vendor/hk-voice/ca.pem", R_OK) == 0 ? "/vendor/hk-voice/ca.pem" : "/etc/ssl/certs/ca-certificates.crt")

/* ---------- small helpers ---------- */

typedef struct {
  char *p;
  size_t n, cap;
} buf_t;

static void die(const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  fputs("hk-voice: ", stderr);
  vfprintf(stderr, fmt, ap);
  fputc('\n', stderr);
  va_end(ap);
  exit(1);
}

static void buf_add(buf_t *b, const void *d, size_t n) {
  if (b->n + n + 1 > b->cap) {
    size_t c = b->cap ? b->cap : 4096;
    while (c < b->n + n + 1) c *= 2;
    char *q = realloc(b->p, c);
    if (!q) die("out of memory");
    b->p = q;
    b->cap = c;
  }
  memcpy(b->p + b->n, d, n);
  b->n += n;
  b->p[b->n] = 0;
}
static void buf_str(buf_t *b, const char *s) { buf_add(b, s, strlen(s)); }
static void buf_fmt(buf_t *b, const char *fmt, ...) {
  char tmp[512];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(tmp, sizeof tmp, fmt, ap);
  va_end(ap);
  if (n > 0) buf_add(b, tmp, (size_t)n < sizeof tmp ? (size_t)n : sizeof tmp - 1);
}

static char *read_file(const char *path, size_t *len) {
  FILE *f = fopen(path, "rb");
  if (!f) return NULL;
  buf_t b = {0};
  char tmp[16384];
  size_t n;
  while ((n = fread(tmp, 1, sizeof tmp, f)) > 0) buf_add(&b, tmp, n);
  fclose(f);
  if (!b.p) buf_add(&b, "", 0);
  if (len) *len = b.n;
  return b.p;
}

static int write_file_atomic(const char *path, const void *d, size_t n, int mode) {
  char tmp[256];
  snprintf(tmp, sizeof tmp, "%s.tmp", path);
  int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, mode);
  if (fd < 0) return -1;
  const char *p = d;
  while (n) {
    ssize_t w = write(fd, p, n);
    if (w <= 0) {
      close(fd);
      unlink(tmp);
      return -1;
    }
    p += w;
    n -= (size_t)w;
  }
  close(fd);
  return rename(tmp, path);
}

/* ---------- config ---------- */

static char conf_buf[4096];
static const char *conf_get(const char *key, const char *def) {
  static int loaded;
  if (!loaded) {
    loaded = 1;
    FILE *f = fopen(getenv("HK_VOICE_CONF") ? getenv("HK_VOICE_CONF") : CONF_PATH, "r");
    if (f) {
      size_t n = fread(conf_buf, 1, sizeof conf_buf - 1, f);
      conf_buf[n] = 0;
      fclose(f);
    }
  }
  size_t kl = strlen(key);
  for (char *line = conf_buf; line && *line;) {
    char *nl = strchr(line, '\n');
    if (strncmp(line, key, kl) == 0 && line[kl] == '=') {
      static char val[8][512];
      static int slot;
      char *v = val[slot++ & 7];
      size_t vl = nl ? (size_t)(nl - line - kl - 1) : strlen(line + kl + 1);
      if (vl >= 512) vl = 511;
      memcpy(v, line + kl + 1, vl);
      v[vl] = 0;
      while (vl && (v[vl - 1] == '\r' || v[vl - 1] == ' ')) v[--vl] = 0;
      return v;
    }
    line = nl ? nl + 1 : NULL;
  }
  return def;
}

/* This kernel pre-dates MemAvailable. Match its low-memory-killer view:
 * anonymous shared pages are not reclaimable even though they count as
 * Cached. */
static long mem_available_kib(void) {
  FILE *f = fopen("/proc/meminfo", "r");
  if (!f) return 0;
  long free_kib = 0, cached_kib = 0, shmem_kib = 0;
  char line[256];
  while (fgets(line, sizeof line, f)) {
    long value;
    if (sscanf(line, "MemFree: %ld", &value) == 1) free_kib = value;
    else if (sscanf(line, "Cached: %ld", &value) == 1) cached_kib = value;
    else if (sscanf(line, "Shmem: %ld", &value) == 1) shmem_kib = value;
  }
  fclose(f);
  long available = free_kib + cached_kib - shmem_kib;
  return available > 0 ? available : 0;
}

static int sequential_reply(void) {
  const char *forced = getenv("HK_VOICE_SEQUENTIAL");
  if (forced && strcmp(forced, "0")) return 1;
  const char *stream = getenv("HK_VOICE_STREAM");
  if (stream && !strcmp(stream, "0")) return 1;
  const char *minimum = getenv("HK_VOICE_STREAM_MIN_KIB");
  long min_kib = minimum ? atol(minimum) : 65536;
  return mem_available_kib() < min_kib;
}

/* ---------- libcurl via dlopen ---------- */

typedef void CURL;
struct curl_slist;
static CURL *(*c_init)(void);
static int (*c_setopt)(CURL *, int, ...);
static int (*c_perform)(CURL *);
static void (*c_cleanup)(CURL *);
static int (*c_getinfo)(CURL *, int, ...);
static const char *(*c_strerror)(int);
static struct curl_slist *(*c_slist_append)(struct curl_slist *, const char *);
static void (*c_slist_free)(struct curl_slist *);
static int (*c_global_init)(long);

enum {
  O_WRITEDATA = 10001, O_URL = 10002, O_POSTFIELDS = 10015, O_USERAGENT = 10018,
  O_HTTPHEADER = 10023, O_CAINFO = 10065, O_WRITEFUNCTION = 20011, O_TIMEOUT = 13,
  O_POSTFIELDSIZE = 60, O_CONNECTTIMEOUT = 78, O_NOSIGNAL = 99, O_POST = 47,
  O_IPRESOLVE = 113, O_HTTPGET = 80, I_TOTAL_TIME = 0x300003, I_NAMELOOKUP = 0x300004, I_CONNECT = 0x300005,
  I_APPCONNECT = 0x300021, I_STARTTRANSFER = 0x300011,
  I_RESPONSE_CODE = 0x200002, I_CONTENT_TYPE = 0x100012
};

static void curl_load(void) {
  if (c_init) return;
  void *h = dlopen("libcurl.so.4", RTLD_NOW);
  if (!h) h = dlopen("/system/workdir/lib/libcurl.so.4", RTLD_NOW);
  if (!h) die("dlopen libcurl: %s", dlerror());
#define SYM(v, n) \
  if (!(*(void **)&v = dlsym(h, n))) die("missing %s", n)
  SYM(c_init, "curl_easy_init");
  SYM(c_setopt, "curl_easy_setopt");
  SYM(c_perform, "curl_easy_perform");
  SYM(c_cleanup, "curl_easy_cleanup");
  SYM(c_getinfo, "curl_easy_getinfo");
  SYM(c_strerror, "curl_easy_strerror");
  SYM(c_slist_append, "curl_slist_append");
  SYM(c_slist_free, "curl_slist_free_all");
  SYM(c_global_init, "curl_global_init");
  c_global_init(3);
}

static size_t on_write(char *d, size_t s, size_t n, void *u) {
  buf_add(u, d, s * n);
  return s * n;
}

static int net_err; /* a request failed at the network level */
static int asr_svc_err, tts_svc_err, token_svc_err; /* Baidu answered, but with a service error */
typedef size_t (*write_fn)(char *, size_t, size_t, void *);
static write_fn http_write_fn = on_write; /* overridable for streaming */
static CURL *http_handle;
static pid_t http_handle_pid;

static void http_close(void) {
  if (http_handle && http_handle_pid == getpid()) c_cleanup(http_handle);
  http_handle = NULL;
  http_handle_pid = 0;
  /* libcurl and OpenSSL retain connection and TLS-buffer allocations. Return
   * them before a low-memory process starts the next network service. */
  malloc_trim(0);
}

/* POST body to url (GET when body is NULL); returns HTTP status, fills resp
 * and content type. */
static long http_post(const char *url, const char **headers, const void *body, size_t len,
                      buf_t *resp, char *ctype, size_t ctlen, long timeout) {
  curl_load();
  /* One handle per process so TLS connections are reused between requests
   * (ASR -> TTS sentences).  A forked child must not touch its parent's
   * handle, so key it on the pid. */
  if (!http_handle || http_handle_pid != getpid()) {
    http_handle = c_init();
    http_handle_pid = getpid();
  }
  CURL *c = http_handle;
  if (!c) die("curl init failed");
  struct curl_slist *hl = NULL;
  for (; headers && *headers; headers++) hl = c_slist_append(hl, *headers);
  c_setopt(c, O_URL, url);
  if (body) {
    c_setopt(c, O_POST, 1L);
    c_setopt(c, O_POSTFIELDS, body);
    c_setopt(c, O_POSTFIELDSIZE, (long)len);
  } else {
    c_setopt(c, O_HTTPGET, 1L);
  }
  c_setopt(c, O_HTTPHEADER, hl);
  c_setopt(c, O_WRITEFUNCTION, http_write_fn);
  c_setopt(c, O_WRITEDATA, resp);
  c_setopt(c, O_CAINFO, CA_FILE);
  c_setopt(c, O_NOSIGNAL, 1L);
  c_setopt(c, O_CONNECTTIMEOUT, 8L);
  c_setopt(c, O_TIMEOUT, timeout);
  c_setopt(c, O_USERAGENT, "hk-voice/1");
  c_setopt(c, O_IPRESOLVE, 1L); /* IPv4 only: the speaker has no routable IPv6 */
  int rc = c_perform(c);
  if (getenv("HK_VOICE_TIMING")) {
    double a = 0, b = 0, t = 0, d = 0, e = 0;
    c_getinfo(c, I_NAMELOOKUP, &a);
    c_getinfo(c, I_CONNECT, &b);
    c_getinfo(c, I_APPCONNECT, &t);
    c_getinfo(c, I_STARTTRANSFER, &d);
    c_getinfo(c, I_TOTAL_TIME, &e);
    const char *hs = strstr(url, "://");
    hs = hs ? hs + 3 : url;
    int hl = (int)strcspn(hs, "/?"); /* host only: never log tokens in the query */
    fprintf(stderr, "hk-voice: timing %.*s dns %.2f tcp %.2f tls %.2f first %.2f total %.2f\n",
            hl, hs, a, b, t, d, e);
  }
  long code = 0;
  if (rc != 0) {
    fprintf(stderr, "hk-voice: request failed: %s\n", c_strerror(rc));
    code = -1;
    net_err = 1;
  } else {
    c_getinfo(c, I_RESPONSE_CODE, &code);
    char *ct = NULL;
    c_getinfo(c, I_CONTENT_TYPE, &ct);
    if (ctype && ctlen) snprintf(ctype, ctlen, "%s", ct ? ct : "");
  }
  c_setopt(c, O_HTTPHEADER, NULL);
  if (hl) c_slist_free(hl);
  if (!resp->p) buf_add(resp, "", 0);
  return code;
}

/* ---------- JSON / URL text helpers ---------- */

static void json_escape(buf_t *b, const char *s) {
  buf_str(b, "\"");
  for (; *s; s++) {
    unsigned char ch = (unsigned char)*s;
    if (ch == '"' || ch == '\\') {
      char e[2] = {'\\', (char)ch};
      buf_add(b, e, 2);
    } else if (ch == '\n') buf_str(b, "\\n");
    else if (ch == '\r') buf_str(b, "\\r");
    else if (ch == '\t') buf_str(b, "\\t");
    else if (ch < 0x20) buf_fmt(b, "\\u%04x", ch);
    else buf_add(b, s, 1);
  }
  buf_str(b, "\"");
}

static void put_utf8(buf_t *b, unsigned cp) {
  char o[4];
  int n;
  if (cp < 0x80) o[0] = cp, n = 1;
  else if (cp < 0x800) o[0] = 0xC0 | cp >> 6, o[1] = 0x80 | (cp & 63), n = 2;
  else if (cp < 0x10000)
    o[0] = 0xE0 | cp >> 12, o[1] = 0x80 | (cp >> 6 & 63), o[2] = 0x80 | (cp & 63), n = 3;
  else
    o[0] = 0xF0 | cp >> 18, o[1] = 0x80 | (cp >> 12 & 63), o[2] = 0x80 | (cp >> 6 & 63),
    o[3] = 0x80 | (cp & 63), n = 4;
  buf_add(b, o, n);
}

static unsigned hex4(const char *p) {
  unsigned v = 0;
  for (int i = 0; i < 4; i++) {
    char c = p[i];
    v <<= 4;
    if (c >= '0' && c <= '9') v |= c - '0';
    else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
    else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
    else return 0xFFFFFFFF;
  }
  return v;
}

/* Decode the JSON string starting at p (just after the opening quote). */
static char *json_unstr(const char *p) {
  buf_t b = {0};
  buf_add(&b, "", 0);
  while (*p && *p != '"') {
    if (*p != '\\') {
      buf_add(&b, p++, 1);
      continue;
    }
    p++;
    switch (*p) {
    case 'n': buf_str(&b, "\n"); p++; break;
    case 't': buf_str(&b, " "); p++; break;
    case 'r': p++; break;
    case 'b': case 'f': p++; break;
    case 'u': {
      unsigned cp = hex4(p + 1);
      if (cp == 0xFFFFFFFF) return b.p;
      p += 5;
      if (cp >= 0xD800 && cp < 0xDC00 && p[0] == '\\' && p[1] == 'u') {
        unsigned lo = hex4(p + 2);
        if (lo >= 0xDC00 && lo < 0xE000) {
          cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
          p += 6;
        }
      }
      put_utf8(&b, cp);
      break;
    }
    case 0: return b.p;
    default: buf_add(&b, p++, 1); break;
    }
  }
  return b.p;
}

/* Find "key": "value" after position `from` and return the decoded value. */
static char *json_find_str(const char *js, const char *key, const char *from) {
  char pat[64];
  snprintf(pat, sizeof pat, "\"%s\"", key);
  const char *p = strstr(from ? from : js, pat);
  while (p) {
    const char *q = p + strlen(pat);
    while (*q == ' ' || *q == '\n' || *q == '\r' || *q == '\t') q++;
    if (*q == ':') {
      q++;
      while (*q == ' ' || *q == '\n' || *q == '\r' || *q == '\t') q++;
      if (*q == '[') {
        q++;
        while (*q == ' ' || *q == '\n') q++;
      }
      if (*q == '"') return json_unstr(q + 1);
      return NULL;
    }
    p = strstr(q, pat);
  }
  return NULL;
}

static long json_find_num(const char *js, const char *key) {
  char pat[64];
  snprintf(pat, sizeof pat, "\"%s\"", key);
  const char *p = strstr(js, pat);
  if (!p) return -999999;
  p = strchr(p + strlen(pat), ':');
  return p ? strtol(p + 1, NULL, 10) : -999999;
}

static void url_encode(buf_t *b, const char *s) {
  static const char hx[] = "0123456789ABCDEF";
  for (; *s; s++) {
    unsigned char ch = (unsigned char)*s;
    if ((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9') ||
        ch == '-' || ch == '_' || ch == '.' || ch == '~')
      buf_add(b, s, 1);
    else {
      char e[3] = {'%', hx[ch >> 4], hx[ch & 15]};
      buf_add(b, e, 3);
    }
  }
}

/* ---------- Baidu ---------- */

static const char *baidu_token(int force) {
  static char tok[256];
  time_t now = time(NULL);
  if (!force) {
    char *t = read_file(TOKEN_PATH, NULL);
    if (t) {
      long exp = strtol(t, NULL, 10);
      char *sp = strchr(t, ' ');
      if (sp && exp > now + 3600 && strlen(sp + 1) < sizeof tok) {
        snprintf(tok, sizeof tok, "%s", sp + 1);
        tok[strcspn(tok, "\r\n")] = 0;
        free(t);
        if (tok[0]) return tok;
      }
      free(t);
    }
  }
  const char *ak = conf_get("BAIDU_API_KEY", NULL), *sk = conf_get("BAIDU_SECRET_KEY", NULL);
  if (!ak || !sk) die("BAIDU_API_KEY/BAIDU_SECRET_KEY missing in config");
  buf_t body = {0}, resp = {0};
  buf_str(&body, "grant_type=client_credentials&client_id=");
  url_encode(&body, ak);
  buf_str(&body, "&client_secret=");
  url_encode(&body, sk);
  const char *hdr[] = {"Content-Type: application/x-www-form-urlencoded", NULL};
  long code = http_post("https://aip.baidubce.com/oauth/2.0/token", hdr, body.p, body.n, &resp,
                        NULL, 0, 15);
  free(body.p);
  char *t = json_find_str(resp.p, "access_token", NULL);
  if (code != 200 || !t || strlen(t) >= sizeof tok) {
    char *e = json_find_str(resp.p, "error_description", NULL);
    fprintf(stderr, "hk-voice: baidu token failed (HTTP %ld): %s\n", code, e ? e : "no token");
    if (code > 0) token_svc_err = 1;
    free(e);
    free(t);
    free(resp.p);
    /* use a stale token if we have one; it may still work */
    char *c = read_file(TOKEN_PATH, NULL);
    char *sp = c ? strchr(c, ' ') : NULL;
    if (sp && strlen(sp + 1) < sizeof tok) {
      snprintf(tok, sizeof tok, "%s", sp + 1);
      tok[strcspn(tok, "\r\n")] = 0;
      free(c);
      return tok;
    }
    free(c);
    return NULL;
  }
  long ttl = json_find_num(resp.p, "expires_in");
  if (ttl < 3600) ttl = 86400;
  snprintf(tok, sizeof tok, "%s", t);
  free(t);
  free(resp.p);
  char line[400];
  int n = snprintf(line, sizeof line, "%ld %s\n", (long)now + ttl, tok);
  write_file_atomic(TOKEN_PATH, line, (size_t)n, 0600);
  return tok;
}

static char *baidu_asr(const int16_t *pcm, size_t samples) {
  for (int attempt = 0; attempt < 2; attempt++) {
    const char *tok = baidu_token(attempt);
    if (!tok) {
      if (token_svc_err) asr_svc_err = 1;
      return NULL;
    }
    buf_t url = {0}, resp = {0};
    buf_fmt(&url, "https://vop.baidu.com/server_api?dev_pid=%s&cuid=hk-allure&token=",
            conf_get("ASR_DEV_PID", "1537"));
    buf_str(&url, tok);
    const char *hdr[] = {"Content-Type: audio/pcm;rate=16000", NULL};
    long code = http_post(url.p, hdr, pcm, samples * 2, &resp, NULL, 0, 20);
    free(url.p);
    long err = json_find_num(resp.p, "err_no");
    if (code == 200 && err == 0) {
      char *r = json_find_str(resp.p, "result", NULL);
      free(resp.p);
      return r;
    }
    char *msg = json_find_str(resp.p, "err_msg", NULL);
    fprintf(stderr, "hk-voice: asr HTTP %ld err_no %ld %s\n", code, err, msg ? msg : "");
    /* 3301 poor audio, 3307 not recognisable, 3308-3312 audio problems are
     * "didn't catch that"; anything else from Baidu is a service problem
     * (3302 auth, 3303 server, 3304 rate, 3305 daily quota, 4/6/17/18/19 ...). */
    if (code > 0 && !(err == 3301 || (err >= 3307 && err <= 3312))) asr_svc_err = 1;
    free(msg);
    free(resp.p);
    /* 3302: token invalid; 3300 range: other errors not fixed by a new token */
    if (err != 3302 && err != 110 && err != 111) break;
  }
  return NULL;
}

/* TTS -> WAV (mono S16; 16 kHz for the basic voices, 24 kHz for newer ones). */
static int baidu_tts(const char *text, buf_t *out) {
  for (int attempt = 0; attempt < 2; attempt++) {
    const char *tok = baidu_token(attempt);
    if (!tok) {
      if (token_svc_err) tts_svc_err = 1;
      return -1;
    }
    buf_t once = {0}, body = {0}, resp = {0};
    url_encode(&once, text);
    buf_str(&body, "tex=");
    url_encode(&body, once.p); /* Baidu asks for the text urlencoded twice */
    free(once.p);
    buf_fmt(&body, "&lan=zh&ctp=1&cuid=hk-allure&aue=6&per=%s&spd=%s&pit=%s&vol=%s&tok=",
            conf_get("TTS_PER", "0"), conf_get("TTS_SPD", "5"), conf_get("TTS_PIT", "5"),
            conf_get("TTS_VOL", "9"));
    buf_str(&body, tok);
    const char *hdr[] = {"Content-Type: application/x-www-form-urlencoded", NULL};
    char ct[128] = "";
    long code = http_post("https://tsn.baidu.com/text2audio", hdr, body.p, body.n, &resp, ct,
                          sizeof ct, 30);
    free(body.p);
    if (code == 200 && strncmp(ct, "audio", 5) == 0 && resp.n > 100) {
      *out = resp;
      return 0;
    }
    long err = json_find_num(resp.p, "err_no");
    char *msg = json_find_str(resp.p, "err_msg", NULL);
    fprintf(stderr, "hk-voice: tts HTTP %ld err_no %ld %s\n", code, err, msg ? msg : "");
    if (code > 0) tts_svc_err = 1;
    free(msg);
    free(resp.p);
    if (err != 502 && err != 3302) break;
  }
  return -1;
}

static long mono_ms(void);

/* ---------- SenseVoice via SiliconFlow (OpenAI-style transcription) ---------- */

static char *sv_asr(const int16_t *pcm, size_t samples, long timeout_s) {
  const char *key = conf_get("SILICONFLOW_KEY", NULL);
  if (!key) return NULL;
  static const char bnd[] = "hkvoiceBoundary7c1e";
  buf_t body = {0}, resp = {0};
  buf_fmt(&body, "--%s\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n%s\r\n", bnd,
          conf_get("SV_MODEL", "FunAudioLLM/SenseVoiceSmall"));
  buf_fmt(&body, "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"q.wav\"\r\n"
                 "Content-Type: audio/wav\r\n\r\n", bnd);
  uint32_t dl = (uint32_t)(samples * 2), rl = dl + 36;
  unsigned char h[44] = {'R','I','F','F', rl, rl >> 8, rl >> 16, rl >> 24, 'W','A','V','E',
                         'f','m','t',' ', 16,0,0,0, 1,0, 1,0, 0x80,0x3e,0,0, 0,0x7d,0,0, 2,0, 16,0,
                         'd','a','t','a', dl, dl >> 8, dl >> 16, dl >> 24};
  buf_add(&body, h, 44);
  buf_add(&body, pcm, samples * 2);
  buf_fmt(&body, "\r\n--%s--\r\n", bnd);
  char auth[300], ct[96];
  snprintf(auth, sizeof auth, "Authorization: Bearer %s", key);
  snprintf(ct, sizeof ct, "Content-Type: multipart/form-data; boundary=%s", bnd);
  const char *hdr[] = {ct, auth, NULL};
  long code = http_post(conf_get("SV_URL", "https://api.siliconflow.cn/v1/audio/transcriptions"),
                        hdr, body.p, body.n, &resp, NULL, 0, timeout_s);
  memset(auth, 0, sizeof auth);
  free(body.p);
  char *t = code == 200 ? json_find_str(resp.p, "text", NULL) : NULL;
  if (code != 200) fprintf(stderr, "hk-voice: sensevoice HTTP %ld\n", code);
  free(resp.p);
  return t;
}

/* Recognise with SenseVoice and Baidu in parallel; prefer SenseVoice when it
 * answers within SV_WAIT seconds (it is more accurate on this far-field mic),
 * otherwise take Baidu's result. */
static char *asr_best(const int16_t *pcm, size_t n) {
  if (!conf_get("SILICONFLOW_KEY", NULL)) return baidu_asr(pcm, n);
  int pfd[2];
  if (pipe(pfd) != 0) return baidu_asr(pcm, n);
  pid_t pid = fork();
  if (pid == 0) {
    close(pfd[0]);
    char *b = baidu_asr(pcm, n);
    if (b && write(pfd[1], b, strlen(b)) < 0) _exit(1);
    _exit(0);
  }
  close(pfd[1]);
  long t0 = mono_ms();
  char *sv = sv_asr(pcm, n, atol(conf_get("SV_WAIT", "4")));
  long tsv = mono_ms() - t0;
  buf_t bd = {0};
  char tmp[1024];
  ssize_t r;
  /* read Baidu's answer (it is short); if SenseVoice won, don't wait long */
  if (sv && *sv) {
    kill(pid, SIGKILL);
  }
  while ((r = read(pfd[0], tmp, sizeof tmp)) > 0) buf_add(&bd, tmp, (size_t)r);
  close(pfd[0]);
  waitpid(pid, NULL, 0);
  fprintf(stderr, "hk-voice: sensevoice \"%s\" (%ld ms), baidu \"%s\"\n", sv ? sv : "-", tsv,
          bd.p ? bd.p : "-");
  if (sv && *sv) {
    free(bd.p);
    return sv;
  }
  free(sv);
  return bd.p;
}

/* ---------- LLM (ModelScope, OpenAI compatible) ---------- */

static void hist_load(char **lines, int *count) {
  struct stat st;
  *count = 0;
  if (stat(HIST_PATH, &st) != 0 || time(NULL) - st.st_mtime > 300) return;
  char *h = read_file(HIST_PATH, NULL);
  if (!h) return;
  for (char *l = strtok(h, "\n"); l && *count < 8; l = strtok(NULL, "\n")) lines[(*count)++] = strdup(l);
  free(h);
}

static void hist_save(char **lines, int count, const char *q, const char *a) {
  buf_t b = {0};
  int start = count > 6 ? count - 6 : 0; /* keep the last three exchanges */
  for (int i = start; i < count; i++) {
    buf_str(&b, lines[i]);
    buf_str(&b, "\n");
  }
  buf_str(&b, "u");
  json_escape(&b, q);
  buf_str(&b, "\na");
  json_escape(&b, a);
  buf_str(&b, "\n");
  mkdir("/tmp/hk-voice", 0700);
  write_file_atomic(HIST_PATH, b.p, b.n, 0600);
  free(b.p);
}

static void llm_auth(char *auth, size_t n) {
  const char *key = conf_get("MODELSCOPE_KEY", NULL);
  if (!key) die("MODELSCOPE_KEY missing in config");
  snprintf(auth, n, "Authorization: Bearer %s", key);
}

static const char *llm_url(void) {
  return conf_get("LLM_URL", "https://api-inference.modelscope.cn/v1/chat/completions");
}

/* Sentence streaming: SSE "data:" lines -> delta content -> sentences on fd. */
typedef struct {
  buf_t line, sent, all;
  FILE *out;
  int in_think, emitted;
} sse_t;
static sse_t *sse_ctx;

static void speakable(char *s, size_t max_bytes);

static void sse_emit(sse_t *x, int force) {
  for (;;) {
    char *s = x->sent.p;
    if (!s || !*s) return;
    /* find the earliest sentence end */
    static const char *ends[] = {"。", "！", "？", "；", "!", "?", "\n", "~", NULL};
    char *cut = NULL;
    size_t cl = 0;
    for (int i = 0; ends[i]; i++) {
      char *e = strstr(s, ends[i]);
      if (e && (!cut || e < cut)) cut = e, cl = strlen(ends[i]);
    }
    if (!x->emitted && (!cut || cut - s > 30)) { /* first chunk: an early comma will do */
      char *e = strstr(s, "，");
      if (e && e - s >= 12) cut = e, cl = 3;
    }
    if (!cut && x->sent.n > 150) { /* long clause: split on a comma */
      char *e = strstr(s + 60, "，");
      if (e) cut = e, cl = 3;
    }
    if (!cut && !force) return;
    size_t n = cut ? (size_t)(cut - s) + cl : x->sent.n;
    char *one = strndup(s, n);
    memmove(s, s + n, x->sent.n - n + 1);
    x->sent.n -= n;
    speakable(one, 900);
    int has = 0;
    static const char *pun[] = {"，", "。", "！", "？", "；", "：", "、", "…", "“", "”", NULL};
    for (char *p = one; *p && !has;) {
      int skip = 0;
      for (int i = 0; pun[i]; i++)
        if (!strncmp(p, pun[i], strlen(pun[i]))) skip = (int)strlen(pun[i]);
      if (skip) p += skip;
      else if (strchr(" ,.!?;:'\"-~()", *p)) p++;
      else has = 1;
    }
    if (has) {
      for (char *p = one; *p; p++)
        if (*p == '\n') *p = ' ';
      fprintf(x->out, "%s\n", one);
      fflush(x->out);
      x->emitted++;
    }
    free(one);
    if (!cut) return;
  }
}

static size_t sse_write(char *d, size_t sz, size_t nm, void *u) {
  (void)u;
  sse_t *x = sse_ctx;
  size_t n = sz * nm;
  buf_add(&x->line, d, n);
  char *nl;
  while ((nl = memchr(x->line.p, '\n', x->line.n))) {
    *nl = 0;
    char *l = x->line.p;
    if (!strncmp(l, "data:", 5) && !strstr(l, "[DONE]")) {
      const char *dl = strstr(l, "\"delta\"");
      char *c = dl ? json_find_str(l, "content", dl) : NULL;
      if (c && *c) {
        buf_str(&x->all, c);
        /* drop <think> blocks if a model emits them */
        char *p = c;
        while (*p) {
          if (x->in_think) {
            char *e = strstr(p, "</think>");
            if (!e) break;
            p = e + 8;
            x->in_think = 0;
          } else {
            char *t = strstr(p, "<think>");
            if (t) {
              buf_add(&x->sent, p, (size_t)(t - p));
              p = t + 7;
              x->in_think = 1;
            } else {
              buf_str(&x->sent, p);
              break;
            }
          }
        }
        sse_emit(x, 0);
      }
      free(c);
    }
    size_t used = (size_t)(nl - x->line.p) + 1;
    memmove(x->line.p, x->line.p + used, x->line.n - used + 1);
    x->line.n -= used;
  }
  return n;
}

static void llm_system(char *sys, size_t n, const char *context) {
  time_t now = time(NULL) + 8 * 3600; /* device clock is UTC; answer in Beijing time */
  struct tm tm;
  gmtime_r(&now, &tm);
  static const char *wd[] = {"日", "一", "二", "三", "四", "五", "六"};
  snprintf(sys, n,
           "%s 现在是北京时间%d年%d月%d日 星期%s %02d:%02d。%s%s",
           conf_get("SYSTEM_PROMPT",
                    "你是哈曼卡顿 Allure 音箱里的中文语音助手。回答会被直接朗读出来，"
                    "所以请用自然口语，直接给出答案，一般一两句话、不超过四十个字，不要寒暄或反问；"
                    "不要使用 Markdown、列表、表格、表情符号或代码。"
                    "用户的话来自远场语音识别，可能有同音字、错字或开头多出的杂词"
                    "（例如把唤醒词 Alexa 识别成“哦那个啥”），请按最可能的本意理解后直接回答；"
                    "只有完全无法理解时，才简短地请对方再说一遍。"
                    "你自己不能设置提醒、闹钟或定时器；如果用户想设置，不要答应，"
                    "请对方用“十分钟后提醒我……”或“明早七点叫我……”这样的说法再说一遍。"),
           tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, wd[tm.tm_wday], tm.tm_hour, tm.tm_min,
           context ? "\n以下是刚查到的实时信息，请据此回答：" : "", context ? context : "");
}

/* Ask the model; when out != NULL, stream and write one sentence per line.
 * Returns the full answer (or NULL). */
static char *llm_ask_ctx(const char *question, const char *context, FILE *out) {
  char sys[3072];
  llm_system(sys, sizeof sys, context);
  char *hist[8];
  int hn;
  hist_load(hist, &hn);

  buf_t body = {0}, resp = {0};
  buf_str(&body, "{\"model\":");
  json_escape(&body, conf_get("MODEL", "Qwen/Qwen3.8-Flash-Next"));
  buf_fmt(&body, ",\"stream\":%s,\"enable_thinking\":false,\"max_tokens\":200,"
                 "\"temperature\":0.7,\"messages\":[{\"role\":\"system\",\"content\":",
          out ? "true" : "false");
  json_escape(&body, sys);
  buf_str(&body, "}");
  for (int i = 0; i < hn; i++) {
    buf_str(&body, hist[i][0] == 'u' ? ",{\"role\":\"user\",\"content\":"
                                     : ",{\"role\":\"assistant\",\"content\":");
    buf_str(&body, hist[i] + 1); /* already JSON-escaped */
    buf_str(&body, "}");
  }
  buf_str(&body, ",{\"role\":\"user\",\"content\":");
  json_escape(&body, question);
  buf_str(&body, "}]}");

  char auth[600];
  llm_auth(auth, sizeof auth);
  const char *hdr[] = {"Content-Type: application/json", auth, NULL};
  char *ans = NULL;
  long code;
  if (out) {
    sse_t x = {0};
    x.out = out;
    buf_add(&x.sent, "", 0);
    buf_add(&x.all, "", 0);
    sse_ctx = &x;
    http_write_fn = sse_write;
    code = http_post(llm_url(), hdr, body.p, body.n, &resp, NULL, 0, 40);
    http_write_fn = on_write;
    sse_emit(&x, 1);
    free(x.line.p);
    free(x.sent.p);
    if (code == 200 && x.all.n) ans = x.all.p;
    else {
      fprintf(stderr, "hk-voice: llm stream HTTP %ld\n", code);
      free(x.all.p);
    }
  } else {
    code = http_post(llm_url(), hdr, body.p, body.n, &resp, NULL, 0, 30);
    const char *ch = strstr(resp.p, "\"message\":{");
    if (!ch) ch = strstr(resp.p, "\"message\": {");
    ans = ch ? json_find_str(resp.p, "content", ch) : NULL;
    if (code != 200 || !ans) {
      char *m = json_find_str(resp.p, "message", NULL);
      fprintf(stderr, "hk-voice: llm HTTP %ld: %s\n", code, m ? m : "(no message)");
      free(m);
      free(ans);
      ans = NULL;
    }
  }
  memset(auth, 0, sizeof auth);
  free(body.p);
  free(resp.p);
  if (ans) {
    char *t;
    while ((t = strstr(ans, "<think>"))) {
      char *e = strstr(t, "</think>");
      if (!e) {
        *t = 0;
        break;
      }
      memmove(t, e + 8, strlen(e + 8) + 1);
    }
    hist_save(hist, hn, question, ans);
  }
  for (int i = 0; i < hn; i++) free(hist[i]);
  return ans;
}

static char *llm_ask(const char *question) { return llm_ask_ctx(question, NULL, NULL); }

/* ---------- weather (open-meteo, no key) ---------- */

static const char *wmo_text(int c) {
  if (c == 0) return "晴";
  if (c <= 2) return "多云";
  if (c == 3) return "阴";
  if (c == 45 || c == 48) return "有雾";
  if (c >= 51 && c <= 57) return "毛毛雨";
  if (c == 61 || c == 80) return "小雨";
  if (c == 63 || c == 81) return "中雨";
  if (c == 65 || c == 82) return "大雨";
  if (c == 66 || c == 67) return "冻雨";
  if (c == 71 || c == 85) return "小雪";
  if (c == 73) return "中雪";
  if (c == 75 || c == 86) return "大雪";
  if (c == 77) return "雪粒";
  if (c >= 95) return "雷阵雨";
  return "天气多变";
}

/* idx-th number of the JSON array "key":[...] found after `from`. */
static double json_arr_num(const char *from, const char *key, int idx, int *ok) {
  char pat[64];
  snprintf(pat, sizeof pat, "\"%s\":[", key);
  const char *p = strstr(from, pat);
  *ok = 0;
  if (!p) return 0;
  p += strlen(pat);
  for (int i = 0; i < idx; i++) {
    p = strchr(p, ',');
    if (!p) return 0;
    p++;
  }
  char *e;
  double v = strtod(p, &e);
  *ok = e != p;
  return v;
}

static int is_weather_question(const char *q) {
  static const char *k[] = {"天气", "气温", "温度", "下雨", "下雪", "冷不冷", "热不热",
                            "带伞", "降温", "多少度", "刮风", "雾霾", NULL};
  for (int i = 0; k[i]; i++)
    if (strstr(q, k[i])) return 1;
  return 0;
}

/* Default weather city: WEATHER_CITY from the config, or located by the
 * public IP (ip-api.com, cached for a day; the coordinates go to GEO_CACHE). */
static void geo_cache_add(const char *city, double lat, double lon);
static const char *default_city(void) {
  static char city[96];
  const char *c = conf_get("WEATHER_CITY", "auto");
  if (strcmp(c, "auto")) return c;
  const char *path = "/tmp/hk-voice/ipcity";
  struct stat st;
  if (stat(path, &st) == 0 && time(NULL) - st.st_mtime < 86400) {
    char *t = read_file(path, NULL);
    if (t && *t) {
      snprintf(city, sizeof city, "%s", t);
      city[strcspn(city, "\n")] = 0;
      free(t);
      return city;
    }
    free(t);
  }
  buf_t r = {0};
  long code = http_post("http://ip-api.com/json/?lang=zh-CN&fields=status,city,lat,lon", NULL,
                        NULL, 0, &r, NULL, 0, 8);
  char *name = code == 200 && r.p && strstr(r.p, "success") ? json_find_str(r.p, "city", NULL) : NULL;
  if (name && *name && strlen(name) < sizeof city) {
    const char *pl = strstr(r.p, "\"lat\":"), *po = strstr(r.p, "\"lon\":");
    snprintf(city, sizeof city, "%s", name);
    if (pl && po) geo_cache_add(city, strtod(pl + 6, NULL), strtod(po + 6, NULL));
    mkdir("/tmp/hk-voice", 0700);
    write_file_atomic(path, city, strlen(city), 0600);
  } else {
    snprintf(city, sizeof city, "%s", "北京");
  }
  free(name);
  free(r.p);
  return city;
}

/* Let the model pick the city/day via a tool call. */
static void weather_args(const char *question, char *city, size_t cn, int *day) {
  snprintf(city, cn, "%s", default_city());
  *day = 0;
  char sys[1024];
  llm_system(sys, sizeof sys, NULL);
  buf_t body = {0}, resp = {0};
  buf_str(&body, "{\"model\":");
  json_escape(&body, conf_get("MODEL", "Qwen/Qwen3.8-Flash-Next"));
  buf_str(&body, ",\"stream\":false,\"enable_thinking\":false,\"max_tokens\":60,"
                 "\"tool_choice\":\"required\",\"messages\":[{\"role\":\"system\",\"content\":");
  char extra[1200];
  snprintf(extra, sizeof extra, "%s 用户没有说城市时，默认城市是%s。", sys, city);
  json_escape(&body, extra);
  buf_str(&body, "},{\"role\":\"user\",\"content\":");
  json_escape(&body, question);
  buf_str(&body, "}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"get_weather\","
                 "\"description\":\"查询城市天气预报\",\"parameters\":{\"type\":\"object\","
                 "\"properties\":{\"city\":{\"type\":\"string\",\"description\":\"中文城市名\"},"
                 "\"day\":{\"type\":\"integer\",\"description\":\"0今天,1明天,2后天\"}},"
                 "\"required\":[\"city\"]}}}]}");
  char auth[600];
  llm_auth(auth, sizeof auth);
  const char *hdr[] = {"Content-Type: application/json", auth, NULL};
  long code = http_post(llm_url(), hdr, body.p, body.n, &resp, NULL, 0, 20);
  memset(auth, 0, sizeof auth);
  free(body.p);
  char *args = code == 200 ? json_find_str(resp.p, "arguments", NULL) : NULL;
  if (args) {
    char *c = json_find_str(args, "city", NULL);
    if (c && *c && strlen(c) < cn) snprintf(city, cn, "%s", c);
    free(c);
    long d = json_find_num(args, "day");
    if (d >= 0 && d <= 2) *day = (int)d;
    free(args);
  }
  free(resp.p);
}

#define GEO_CACHE "/vendor/hk-voice/geo.cache"

static void geo_cache_add(const char *city, double lat, double lon) {
  char *c = read_file(GEO_CACHE, NULL);
  size_t cl = strlen(city);
  int have = 0;
  for (char *l = c; l && *l;) {
    if (!strncmp(l, city, cl) && l[cl] == ' ') have = 1;
    char *nl = strchr(l, '\n');
    l = nl ? nl + 1 : NULL;
  }
  free(c);
  if (have) return;
  FILE *f = fopen(GEO_CACHE, "a");
  if (f) {
    fprintf(f, "%s %.4f %.4f\n", city, lat, lon);
    fclose(f);
  }
}

/* City -> coordinates, cached for good in GEO_CACHE ("city lat lon"). */
static int geocode(const char *city, double *lat, double *lon) {
  char *c = read_file(GEO_CACHE, NULL);
  size_t cl = strlen(city);
  for (char *l = c ? strtok(c, "\n") : NULL; l; l = strtok(NULL, "\n"))
    if (!strncmp(l, city, cl) && l[cl] == ' ' && sscanf(l + cl, "%lf %lf", lat, lon) == 2) {
      free(c);
      return 0;
    }
  free(c);
  buf_t u = {0}, r = {0};
  buf_str(&u, "https://geocoding-api.open-meteo.com/v1/search?count=1&language=zh&name=");
  url_encode(&u, city);
  long code = http_post(u.p, NULL, NULL, 0, &r, NULL, 0, 10);
  free(u.p);
  const char *res = r.p ? strstr(r.p, "\"results\"") : NULL;
  *lat = *lon = 0;
  if (code == 200 && res) {
    const char *pl = strstr(res, "\"latitude\":"), *po = strstr(res, "\"longitude\":");
    if (pl && po) *lat = strtod(pl + 11, NULL), *lon = strtod(po + 12, NULL);
  }
  free(r.p);
  if (*lat == 0 && *lon == 0) return -1;
  geo_cache_add(city, *lat, *lon);
  return 0;
}

/* Forecast JSON for a city, cached in /tmp for 30 minutes. */
static char *forecast_json(const char *city, int refresh) {
  unsigned h = 5381;
  for (const char *p = city; *p; p++) h = h * 33 + (unsigned char)*p;
  char path[128];
  mkdir("/tmp/hk-voice", 0700);
  snprintf(path, sizeof path, "/tmp/hk-voice/wx-%08x.json", h);
  struct stat st;
  if (!refresh && stat(path, &st) == 0 && time(NULL) - st.st_mtime < 1800) {
    char *j = read_file(path, NULL);
    if (j && strstr(j, "\"daily\":{")) return j;
    free(j);
  }
  double lat, lon;
  if (geocode(city, &lat, &lon) != 0) return NULL;
  buf_t w = {0}, r = {0};
  buf_fmt(&w, "https://api.open-meteo.com/v1/forecast?latitude=%.3f&longitude=%.3f", lat, lon);
  buf_str(&w, "&current=temperature_2m,weather_code&daily=weather_code,temperature_2m_max,"
              "temperature_2m_min,precipitation_probability_max&timezone=Asia%2FShanghai&forecast_days=3");
  long code = http_post(w.p, NULL, NULL, 0, &r, NULL, 0, 10);
  free(w.p);
  if (code != 200 || !r.p || !strstr(r.p, "\"daily\":{")) {
    free(r.p);
    return NULL;
  }
  write_file_atomic(path, r.p, r.n, 0600);
  return r.p;
}

static char *weather_report(const char *city, int day) {
  int ok1, ok2;
  buf_t r = {0};
  r.p = forecast_json(city, 0);
  const char *daily = r.p ? strstr(r.p, "\"daily\":{") : NULL;
  if (!daily) {
    free(r.p);
    return NULL;
  }
  buf_t out = {0};
  static const char *dn[] = {"今天", "明天", "后天"};
  const char *cur = strstr(r.p, "\"current\":{");
  if (cur && day == 0) {
    const char *t = strstr(cur, "\"temperature_2m\":");
    const char *c = strstr(cur, "\"weather_code\":");
    if (t && c)
      buf_fmt(&out, "%s现在%s，%.0f度。", city, wmo_text(atoi(c + 15)), strtod(t + 17, NULL));
  }
  for (int i = day; i <= 2 && i <= day + 1; i++) {
    double code_d = json_arr_num(daily, "weather_code", i, &ok1);
    double hi = json_arr_num(daily, "temperature_2m_max", i, &ok2);
    double lo = json_arr_num(daily, "temperature_2m_min", i, &ok1);
    int okp;
    double pp = json_arr_num(daily, "precipitation_probability_max", i, &okp);
    if (!ok2) continue;
    buf_fmt(&out, "%s%s%s，%.0f到%.0f度", city, dn[i], wmo_text((int)code_d), lo, hi);
    if (okp) buf_fmt(&out, "，降水概率%.0f%%", pp);
    buf_str(&out, "。");
  }
  free(r.p);
  return out.p;
}

/* ---------- local commands (no model round trip) ---------- */

static const char *local_action(const char *q) {
  struct { const char *word, *action; } m[] = {
      {"下一首", "next"}, {"切歌", "next"}, {"换一首", "next"}, {"上一首", "prev"},
      {"暂停", "pause"}, {"停止播放", "pause"}, {"别放了", "pause"}, {"停一下", "pause"},
      {"继续播放", "play"}, {"接着放", "play"}, {"恢复播放", "play"}, {"继续放", "play"},
      {"大声", "volup"}, {"声音大一点", "volup"}, {"音量大", "volup"}, {"调高音量", "volup"},
      {"音量调高", "volup"}, {"大点声", "volup"},
      {"小声", "voldown"}, {"声音小一点", "voldown"}, {"音量小", "voldown"},
      {"调低音量", "voldown"}, {"音量调低", "voldown"}, {"小点声", "voldown"},
      {"没事了", "bye"}, {"不用了", "bye"}, {"再见", "bye"}, {"退下", "bye"}, {"没有了", "bye"},
      {NULL, NULL}};
  size_t ql = strlen(q);
  if (ql > 30) return NULL; /* commands are short; longer text goes to the model */
  for (int i = 0; m[i].word; i++)
    if (strstr(q, m[i].word)) return m[i].action;
  return NULL;
}

/* Make LLM text speakable: drop markdown symbols, collapse whitespace,
 * cap the length on a character boundary. */
static void speakable(char *s, size_t max_bytes) {
  char *w = s;
  int space = 0;
  for (char *r = s; *r; r++) {
    unsigned char c = (unsigned char)*r;
    if (c == '*' || c == '#' || c == '`' || c == '_' || c == '|' || c == '>' || c == '~') continue;
    if (c == '\n' || c == '\t' || c == ' ' || c == '\r') {
      if (!space && w != s) *w++ = '\n' == c ? ',' : ' ';
      space = 1;
      continue;
    }
    space = 0;
    *w++ = (char)c;
  }
  *w = 0;
  if (strlen(s) > max_bytes) {
    size_t n = max_bytes;
    while (n && ((unsigned char)s[n] & 0xC0) == 0x80) n--;
    s[n] = 0;
  }
}

/* ---------- audio ---------- */

static const float lp_in[31] = {0.0011515f,0.0020108f,0.0011360f,-0.0025013f,-0.0066988f,-0.0051312f,0.0060885f,0.0193677f,0.0171422f,-0.0106377f,-0.0470584f,-0.0504461f,0.0143851f,0.1389199f,0.2639760f,0.3165911f,0.2639760f,0.1389199f,0.0143851f,-0.0504461f,-0.0470584f,-0.0106377f,0.0171422f,0.0193677f,0.0060885f,-0.0051312f,-0.0066988f,-0.0025013f,0.0011360f,0.0020108f,0.0011515f};
static const float lp_out[31] = {0.0042236f,0.0056601f,0.0017119f,-0.0094204f,-0.0197190f,-0.0112990f,0.0233915f,0.0585656f,0.0444549f,-0.0414388f,-0.1438584f,-0.1427444f,0.0564963f,0.4224755f,0.7839696f,0.9350619f,0.7839696f,0.4224755f,0.0564963f,-0.1427444f,-0.1438584f,-0.0414388f,0.0444549f,0.0585656f,0.0233915f,-0.0112990f,-0.0197190f,-0.0094204f,0.0017119f,0.0056601f,0.0042236f};

static int16_t clip16(float v) {
  if (v > 32767.f) return 32767;
  if (v < -32768.f) return -32768;
  return (int16_t)(v >= 0 ? v + 0.5f : v - 0.5f);
}

static size_t mic_convert(const int16_t *s, size_t frames, unsigned ch, unsigned rate,
                          double skip_s, int use_ch, int16_t **out, float *peak_out);

/* Load the stock recording and produce 16 kHz mono with gain normalisation.
 * Returns the number of samples, 0 when the file holds no speech-level audio. */
static size_t load_mic(const char *path, int16_t **out, float *peak_out) {
  size_t len;
  unsigned char *d = (unsigned char *)read_file(path, &len);
  if (!d || len < 44 || memcmp(d, "RIFF", 4) || memcmp(d + 8, "WAVE", 4)) die("not a WAV file: %s", path);
  unsigned ch = 2, rate = 44100, bits = 16;
  size_t off = 12, data = 0, dlen = 0;
  while (off + 8 <= len) {
    uint32_t sz = d[off + 4] | d[off + 5] << 8 | d[off + 6] << 16 | (uint32_t)d[off + 7] << 24;
    if (!memcmp(d + off, "fmt ", 4) && off + 24 <= len) {
      ch = d[off + 10] | d[off + 11] << 8;
      rate = d[off + 12] | d[off + 13] << 8 | d[off + 14] << 16 | (uint32_t)d[off + 15] << 24;
      bits = d[off + 22] | d[off + 23] << 8;
    } else if (!memcmp(d + off, "data", 4)) {
      data = off + 8;
      dlen = len - data; /* the stock writer's sizes are unreliable */
      break;
    }
    off += 8 + sz + (sz & 1);
  }
  if (!data || bits != 16 || ch < 1 || ch > 2 || rate < 16000) die("unsupported WAV format");
  size_t frames = dlen / (2 * ch);
  size_t n = mic_convert((const int16_t *)(d + data), frames, ch, rate, 0.1, -1, out, peak_out);
  free(d);
  return n;
}

/* Mic samples -> 16 kHz mono, gain normalised.  use_ch < 0: pick the louder
 * channel (on this board one channel is the microphone, the other is nearly
 * silent).  skip_s: seconds dropped from the start. */
static size_t mic_convert(const int16_t *s, size_t frames, unsigned ch, unsigned rate,
                          double skip_s, int use_ch, int16_t **out, float *peak_out) {

  /* Pick the louder channel: on this board one channel is the microphone,
   * the other is nearly silent. */
  int use = use_ch >= 0 ? use_ch : 0;
  if (ch == 2 && use_ch < 0) {
    double e0 = 0, e1 = 0;
    for (size_t i = 0; i < frames; i++) e0 += (double)s[2 * i] * s[2 * i], e1 += (double)s[2 * i + 1] * s[2 * i + 1];
    use = e1 > e0;
  }
  float *x = malloc(frames * sizeof *x);
  if (!x) die("out of memory");
  double mean = 0;
  for (size_t i = 0; i < frames; i++) mean += s[i * ch + use];
  mean /= frames ? frames : 1;
  for (size_t i = 0; i < frames; i++) x[i] = (float)(s[i * ch + use] - mean);

  double step = (double)rate / 16000.0;
  size_t n_out = (size_t)(frames / step);
  int16_t *o = malloc((n_out + 1) * sizeof *o);
  float *y = malloc((n_out + 1) * sizeof *y);
  if (!o || !y) die("out of memory");
  size_t n = 0;
  float peak = 0;
  for (double pos = rate * skip_s; pos + 16 < frames; pos += step) {
    long i0 = (long)pos;
    float fr = (float)(pos - i0), a = 0, b = 0;
    for (int k = 0; k < 31; k++) {
      long j = i0 - 15 + k;
      if (j >= 0) a += lp_in[k] * x[j], b += lp_in[k] * x[j + 1];
    }
    float v = a + (b - a) * fr;
    y[n++] = v;
    if (v > peak) peak = v;
    if (-v > peak) peak = -v;
  }
  free(x);
  float gain = peak > 1 ? 16000.f / peak : 1;
  if (gain > 40) gain = 40;
  if (gain < 1) gain = 1;
  for (size_t i = 0; i < n; i++) o[i] = clip16(y[i] * gain);
  free(y);
  *out = o;
  if (peak_out) *peak_out = peak;
  return n;
}

/* ---------- noise reduction (spectral subtraction, no libm) ----------
 * Stationary noise such as a range hood or fan: per-bin noise power is the
 * minimum of the smoothed spectrum over the clip (speech has gaps), and
 * each bin is attenuated by a floored Wiener-style gain.  512-point FFT,
 * 50% overlap, sqrt-Hann analysis/synthesis windows (perfect reconstruction).
 */
#define NR_N 512
#define NR_H 256

static float nr_sqrt(float v) {
  if (v <= 0) return 0;
  float x = v > 1 ? v : 1;
  for (int i = 0; i < 12; i++) x = 0.5f * (x + v / x);
  return x;
}

static float nr_cos[NR_N / 2], nr_sin[NR_N / 2], nr_win[NR_N];

static void nr_tables(void) {
  double c = 1, s = 0;
  const double c1 = 0.999924701839, s1 = 0.012271538286; /* 2pi/512 */
  for (int k = 0; k < NR_N / 2; k++) {
    nr_cos[k] = (float)c, nr_sin[k] = (float)s;
    double t = c * c1 - s * s1;
    s = s * c1 + c * s1;
    c = t;
  }
  /* win[k] = sin(pi (k + 0.5) / N) */
  double wc = 0.999995293810, ws = 0.003067956763; /* angle pi/1024 */
  const double r1 = 0.999981175283, r2 = 0.006135884649; /* step pi/512 */
  for (int k = 0; k < NR_N; k++) {
    nr_win[k] = (float)ws;
    double t = wc * r1 - ws * r2;
    ws = ws * r1 + wc * r2;
    wc = t;
  }
}

static void nr_fft(float *re, float *im, int inverse) {
  for (int i = 1, j = 0; i < NR_N; i++) {
    int bit = NR_N >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      float t = re[i]; re[i] = re[j]; re[j] = t;
      t = im[i]; im[i] = im[j]; im[j] = t;
    }
  }
  for (int len = 2; len <= NR_N; len <<= 1) {
    int step = NR_N / len;
    for (int i = 0; i < NR_N; i += len)
      for (int k = 0; k < len / 2; k++) {
        float wr = nr_cos[k * step], wi = inverse ? nr_sin[k * step] : -nr_sin[k * step];
        int a = i + k, b = a + len / 2;
        float xr = re[b] * wr - im[b] * wi, xi = re[b] * wi + im[b] * wr;
        re[b] = re[a] - xr, im[b] = im[a] - xi;
        re[a] += xr, im[a] += xi;
      }
  }
}

static void denoise(int16_t *x, size_t n) {
  if (n < NR_N * 4) return;
  nr_tables();
  size_t frames = (n - NR_N) / NR_H + 1;
  int bins = NR_N / 2 + 1;
  float *pw = malloc(frames * bins * sizeof *pw);
  float *re = malloc(NR_N * sizeof *re), *im = malloc(NR_N * sizeof *im);
  float *noise = malloc(bins * sizeof *noise), *gprev = calloc(bins, sizeof *gprev);
  float *out = calloc(n, sizeof *out);
  if (!pw || !re || !im || !noise || !gprev || !out) return;
  /* pass 1: power spectra */
  for (size_t f = 0; f < frames; f++) {
    for (int k = 0; k < NR_N; k++) re[k] = x[f * NR_H + k] * nr_win[k], im[k] = 0;
    nr_fft(re, im, 0);
    for (int b = 0; b < bins; b++) pw[f * bins + b] = re[b] * re[b] + im[b] * im[b];
  }
  /* noise: minimum over time of the 5-frame smoothed power, with bias */
  for (int b = 0; b < bins; b++) {
    float mn = -1;
    for (size_t f = 2; f + 2 < frames; f++) {
      float sm = (pw[(f - 2) * bins + b] + pw[(f - 1) * bins + b] + pw[f * bins + b] +
                  pw[(f + 1) * bins + b] + pw[(f + 2) * bins + b]) / 5;
      if (mn < 0 || sm < mn) mn = sm;
    }
    noise[b] = (mn < 0 ? 0 : mn) * 2.0f;
  }
  /* pass 2: gains, inverse FFT, overlap-add */
  const float alpha = getenv("NR_A") ? (float)atof(getenv("NR_A")) : 1.0f,
              beta = getenv("NR_B") ? (float)atof(getenv("NR_B")) : 0.35f;
  for (size_t f = 0; f < frames; f++) {
    for (int k = 0; k < NR_N; k++) re[k] = x[f * NR_H + k] * nr_win[k], im[k] = 0;
    nr_fft(re, im, 0);
    for (int b = 0; b < bins; b++) {
      float p = pw[f * bins + b];
      float g2 = p > 0 ? 1 - alpha * noise[b] / p : 0;
      if (g2 < beta * beta) g2 = beta * beta;
      float g = nr_sqrt(g2);
      g = 0.3f * gprev[b] + 0.7f * g; /* light smoothing against musical noise */
      gprev[b] = g;
      re[b] *= g, im[b] *= g;
      if (b > 0 && b < NR_N / 2) re[NR_N - b] = re[b], im[NR_N - b] = -im[b];
    }
    nr_fft(re, im, 1);
    for (int k = 0; k < NR_N; k++) out[f * NR_H + k] += re[k] / NR_N * nr_win[k];
  }
  float peak = 0;
  for (size_t i = 0; i < n; i++) {
    float v = out[i] < 0 ? -out[i] : out[i];
    if (v > peak) peak = v;
  }
  float g = peak > 1 ? 16000.f / peak : 1;
  if (g > 8) g = 8;
  for (size_t i = 0; i < n; i++) x[i] = clip16(out[i] * g);
  free(pw), free(re), free(im), free(noise), free(gprev), free(out);
}

/* AirPlay volume (-30..0, -144 mute) -> output attenuation in dB, following
 * Shairport's dasl_tapered profile with the deployed max of -6 dB. */
static float log2f_approx(float v) {
  int e = 0;
  while (v < 1) v *= 2, e--;
  while (v >= 2) v /= 2, e++;
  float r = 0, bit = 0.5f;
  for (int i = 0; i < 16; i++) {
    v *= v;
    if (v >= 2) v /= 2, r += bit;
    bit /= 2;
  }
  return e + r;
}

static char *unix_request(const char *path, const char *cmd) {
  int fd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (fd < 0) return NULL;
  struct sockaddr_un a;
  memset(&a, 0, sizeof a);
  a.sun_family = AF_UNIX;
  snprintf(a.sun_path, sizeof a.sun_path, "%s", path);
  if (connect(fd, (struct sockaddr *)&a, sizeof a) != 0) {
    close(fd);
    return NULL;
  }
  if (write(fd, cmd, strlen(cmd) + 1) < 0) {
    close(fd);
    return NULL;
  }
  shutdown(fd, SHUT_WR);
  buf_t b = {0};
  char tmp[2048];
  ssize_t n;
  while ((n = read(fd, tmp, sizeof tmp)) > 0) buf_add(&b, tmp, (size_t)n);
  close(fd);
  return b.p;
}

/* Is an AirPlay sender connected (RTSP on :7000 established)? */
static int ap2_connected(void) {
  char *t = read_file("/proc/net/tcp", NULL), *t6 = read_file("/proc/net/tcp6", NULL);
  int c = 0;
  for (char *src = t; src; src = (src == t ? t6 : NULL)) {
    for (char *l = strtok(src, "\n"); l; l = strtok(NULL, "\n")) {
      char *p = strchr(l, ':');
      if (!p) continue;
      p = strchr(p + 1, ' '); /* after local address */
      if (p && strstr(l, ":1B58 ") && strstr(l, ":1B58 ") < p && strstr(p, " 01 ")) c = 1;
    }
    if (src == t6) break;
  }
  free(t);
  free(t6);
  return c;
}

/* Reply loudness follows the speaker volume: the AirPlay slider while a
 * sender is connected, the stock volume (0..100) otherwise.  GAIN_REF_DB is
 * the level at volume 25 (what the user picked); each doubling adds 6 dB. */
static float output_gain_db(void) {
  const char *bt_mix = getenv("HK_VOICE_BT_INJECT_ACTIVE");
  if (bt_mix && access(bt_mix, F_OK) == 0) return -12.0f;
  const char *fixed = conf_get("GAIN_DB", NULL);
  if (fixed) return (float)strtod(fixed, NULL);
  float ref = (float)strtod(conf_get("GAIN_REF_DB", "-20"), NULL);
  float pct = -1;
  if (ap2_connected()) {
    char *log = read_file(VOLUME_LOG, NULL);
    char *last = NULL;
    if (log)
      for (char *l = strtok(log, "\n"); l; l = strtok(NULL, "\n")) last = l;
    if (last) {
      char *sp = strchr(last, ' ');
      float v = (float)strtod(sp ? sp + 1 : last, NULL);
      pct = v < -30 ? 0 : (v + 30) / 30 * 100;
    }
    free(log);
  }
  if (pct < 0) {
    char *st = unix_request("/tmp/Requesta01controller", "getPlayerStatus");
    char *v = st ? strstr(st, "\"vol\":\"") : NULL;
    if (v) pct = (float)atoi(v + 7);
    free(st);
  }
  if (pct < 0) return ref;
  if (pct < 3) pct = 3; /* still audible when the speaker is turned right down */
  float g = ref + 6 * log2f_approx(pct / 25.f);
  if (g > -6) g = -6;
  if (g < -40) g = -40;
  return g;
}

static float db_to_lin(float db) {
  float g = 1;
  const float step = 1.12201845f; /* 10^(1/20) */
  int whole = (int)(db >= 0 ? db : -db);
  for (int i = 0; i < whole; i++) g = db >= 0 ? g * step : g / step;
  return g;
}

static const float lp_out2[31] = {0.0033237f,0.0015641f,-0.0048674f,-0.0062846f,0.0074517f,0.0181981f,-0.0054798f,-0.0390707f,-0.0104142f,0.0667409f,0.0543637f,-0.0952287f,-0.1606349f,0.1167441f,0.6169299f,0.8733282f,0.6169299f,0.1167441f,-0.1606349f,-0.0952287f,0.0543637f,0.0667409f,-0.0104142f,-0.0390707f,-0.0054798f,0.0181981f,0.0074517f,-0.0062846f,-0.0048674f,0.0015641f,0.0033237f};

/* Mono S16 at `rate` -> 48 kHz stereo S16 with gain and short silent margins. */
static int write_output(const char *path, const int16_t *s, size_t n, unsigned rate, float gain_db) {
  float g = db_to_lin(gain_db);
  unsigned L = (rate && 48000 % rate == 0) ? 48000 / rate : 0;
  const float *h = L == 3 ? lp_out : L == 2 ? lp_out2 : NULL;
  size_t m_total = h ? n * L : (size_t)((double)n * 48000 / (rate ? rate : 16000));
  size_t lead = 48000 / 10, tail = 48000 * 3 / 10;
  size_t frames = lead + m_total + tail;
  int16_t *o = calloc(frames * 2, sizeof *o);
  if (!o) return -1;
  for (size_t m = 0; m < m_total; m++) {
    float acc = 0;
    if (h) {
      /* zero-stuffed interpolation: only every L-th tap is non-zero */
      for (int k = 0; k < 31; k++) {
        long t = (long)m - 15 + k;
        if (t >= 0 && t % L == 0 && (size_t)(t / L) < n) acc += h[k] * s[t / L];
      }
    } else {
      double p = (double)m * rate / 48000;
      size_t j = (size_t)p;
      float f = (float)(p - j);
      acc = s[j] * (1 - f) + (j + 1 < n ? s[j + 1] : 0) * f;
    }
    float env = 1; /* 10 ms fade-in/out on the speech itself */
    if (m < 480) env = m / 480.f;
    if (m_total - m < 480) env = (m_total - m) / 480.f;
    int16_t v = clip16(acc * g * env);
    o[2 * (lead + m)] = v;
    o[2 * (lead + m) + 1] = v;
  }
  int rc = write_file_atomic(path, o, frames * 4, 0644);
  free(o);
  return rc;
}

static int wav_to_output(buf_t w, const char *out, float g);

static int tts_to_file(const char *text, const char *out, float g) {
  buf_t w = {0};
  if (baidu_tts(text, &w) != 0) return -1;
  return wav_to_output(w, out, g);
}

/* Pre-synthesised prompts in CLIP_DIR, for when the network is down. */
#define CLIP_DIR "/vendor/hk-voice/clips"
static const struct { const char *name, *text; } clips[] = {
    {"neterr", "网络好像断了，请稍后再试。"},
    {"unclear", "没听清，请再说一遍。"},
    {"llmerr", "抱歉，我暂时连不上大模型，请稍后再试。"},
    {"remind", "提醒时间到了。"},
    {"ok", "好的。"},
    {"asrerr", "语音识别服务出错了，请稍后再试。"},
    {"ttserr", "语音合成服务出错了，请稍后再试。"},
    {NULL, NULL}};

static int clip_to_file(const char *name, const char *out, float g) {
  char path[256];
  snprintf(path, sizeof path, "%s/%s.wav", CLIP_DIR, name);
  buf_t w = {0};
  w.p = read_file(path, &w.n);
  if (!w.p) return -1;
  return wav_to_output(w, out, g);
}

static int make_clips(void) {
  mkdir(CLIP_DIR, 0755);
  int bad = 0;
  for (int i = 0; clips[i].name; i++) {
    buf_t w = {0};
    char path[256];
    snprintf(path, sizeof path, "%s/%s.wav", CLIP_DIR, clips[i].name);
    if (baidu_tts(clips[i].text, &w) != 0 || write_file_atomic(path, w.p, w.n, 0644) != 0) bad++;
    free(w.p);
  }
  return bad;
}

/* Queue one clip (or text when the network works) as the whole reply. */
static int queue_reply(const char *qdir, const char *text, const char *clip) {
  char tmp[300], fin[300];
  float g = output_gain_db();
  snprintf(tmp, sizeof tmp, "%s/q.tmp", qdir);
  int ok = (text && !net_err && tts_to_file(text, tmp, g) == 0) ||
           (text && tts_svc_err && !net_err && clip_to_file("ttserr", tmp, g) == 0) ||
           (clip && clip_to_file(clip, tmp, g) == 0);
  if (ok) {
    snprintf(fin, sizeof fin, "%s/q.1.raw", qdir);
    rename(tmp, fin);
  }
  snprintf(fin, sizeof fin, "%s/q.end", qdir);
  int fd = open(fin, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (fd >= 0) close(fd);
  return ok ? 0 : 1;
}

static int wav_to_output(buf_t w, const char *out, float g) {
  const unsigned char *d = (const unsigned char *)w.p;
  unsigned rate = 16000;
  size_t off = 12, data = 0, len = w.n;
  if (len > 44 && !memcmp(d, "RIFF", 4)) {
    while (off + 8 <= len) {
      uint32_t sz = d[off + 4] | d[off + 5] << 8 | d[off + 6] << 16 | (uint32_t)d[off + 7] << 24;
      if (!memcmp(d + off, "fmt ", 4) && off + 16 <= len)
        rate = d[off + 12] | d[off + 13] << 8 | d[off + 14] << 16 | (uint32_t)d[off + 15] << 24;
      else if (!memcmp(d + off, "data", 4)) {
        data = off + 8;
        break;
      }
      off += 8 + sz + (sz & 1);
    }
  }
  if (!data) data = 0; /* raw PCM */
  size_t n = (len - data) / 2;
  int16_t *pcm = malloc(n * 2 + 2);
  if (!pcm) return -1;
  memcpy(pcm, d + data, n * 2);
  int rc = write_output(out, pcm, n, rate, g);
  fprintf(stderr, "hk-voice: tts %zu ms at %u Hz, gain %.1f dB\n", n * 1000 / rate, rate, g);
  free(pcm);
  free(w.p);
  return rc;
}

static int say_to(const char *text, const char *out) { return tts_to_file(text, out, output_gain_db()); }

/* Keep the DAC stream running: write silence to stdout (piped into aplay)
 * until <ready> appears, play it, add trailing silence, touch <played>, and
 * stop when <stop> appears.  The speaker path is switched only while this
 * stream carries silence, which avoids the I2S start/stop pops. */
static int feed(const char *ready, const char *played, const char *stop) {
  static int16_t zero[960 * 2]; /* 20 ms at 48 kHz stereo */
  signal(SIGPIPE, SIG_DFL);
  for (long iter = 0;; iter++) {
    if (access(stop, F_OK) == 0) return 0;
    if (iter > 60L * 50 * 5) return 1; /* hard cap: 5 minutes */
    if (access(ready, F_OK) == 0) {
      size_t len;
      char *a = read_file(ready, &len);
      unlink(ready);
      if (a) {
        if (fwrite(a, 1, len & ~(size_t)3, stdout) != (len & ~(size_t)3)) return 1;
        free(a);
      }
      /* pipe (64 KiB) + ALSA buffer hold ~0.9 s; cover it with silence */
      for (int i = 0; i < 75; i++) fwrite(zero, 1, sizeof zero, stdout);
      fflush(stdout);
      int fd = open(played, O_WRONLY | O_CREAT | O_TRUNC, 0644);
      if (fd >= 0) close(fd);
      continue;
    }
    if (fwrite(zero, 1, sizeof zero, stdout) != sizeof zero) return 1;
    fflush(stdout);
  }
}

/* Short two-note chime (E6, A6) as 48 kHz stereo; recursive oscillator. */
static int write_tone(const char *path, float gain_db) {
  const float c2[2] = {1.970286131f, 1.947157806f}, sn[2] = {0.171735671f, 0.228350870f};
  const int len[2] = {48000 * 9 / 100, 48000 * 16 / 100};
  size_t frames = 4800 + len[0] + len[1] + 9600;
  int16_t *o = calloc(frames * 2, sizeof *o);
  if (!o) return -1;
  float g = db_to_lin(gain_db) * 9000;
  size_t pos = 4800;
  for (int k = 0; k < 2; k++) {
    float y1 = 0, y2 = -sn[k]; /* y[-1]=0, y[-2]=-sin(w) -> y[n]=sin(w n) */
    for (int i = 0; i < len[k]; i++) {
      float y = c2[k] * y1 - y2;
      y2 = y1;
      y1 = y;
      float env = 1;
      if (i < 240) env = i / 240.f;
      int rem = len[k] - i;
      if (rem < 1500) env = env * rem / 1500.f;
      int16_t v = clip16(y * g * env);
      o[2 * pos] = o[2 * pos + 1] = v;
      pos++;
    }
  }
  int rc = write_file_atomic(path, o, frames * 4, 0644);
  free(o);
  return rc;
}

/* Queue feeder: stream silence to stdout (into aplay) and play <dir>/q.N.raw
 * in order as they appear.  <dir>/q.end marks the end of a reply (trailing
 * silence, then <dir>/played); <dir>/q.abort drops the queue; <dir>/stop ends
 * the stream.  The speaker path is only switched while silence flows. */
static int feedq(const char *dir) {
  static int16_t zero[960 * 2]; /* 20 ms */
  char path[256], f_end[256], f_abort[256], f_stop[256], f_played[256], f_started[256];
  snprintf(f_end, sizeof f_end, "%s/q.end", dir);
  snprintf(f_abort, sizeof f_abort, "%s/q.abort", dir);
  snprintf(f_stop, sizeof f_stop, "%s/stop", dir);
  snprintf(f_played, sizeof f_played, "%s/played", dir);
  snprintf(f_started, sizeof f_started, "%s/started", dir);
  signal(SIGPIPE, SIG_DFL);
  int next = 1;
  for (long iter = 0;; iter++) {
    if (access(f_stop, F_OK) == 0) return 0;
    if (iter > 60L * 50 * 10) return 1; /* hard cap: 10 minutes */
    if (access(f_abort, F_OK) == 0) {
      for (int i = next; i < next + 64; i++) {
        snprintf(path, sizeof path, "%s/q.%d.raw", dir, i);
        unlink(path);
      }
      unlink(f_abort);
      unlink(f_end);
      next = 1;
      int fd = open(f_played, O_WRONLY | O_CREAT | O_TRUNC, 0644);
      if (fd >= 0) close(fd);
    }
    snprintf(path, sizeof path, "%s/chime.raw", dir);
    if (access(path, F_OK) == 0) { /* acknowledgement chime, played once */
      size_t len;
      char *a = read_file(path, &len);
      unlink(path);
      if (a && fwrite(a, 1, len & ~(size_t)3, stdout) != (len & ~(size_t)3)) return 1;
      free(a);
      continue;
    }
    snprintf(path, sizeof path, "%s/q.%d.raw", dir, next);
    if (access(path, F_OK) == 0) {
      size_t len;
      char *a = read_file(path, &len);
      unlink(path);
      if (next == 1) {
        int fd = open(f_started, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd >= 0) close(fd);
      }
      next++;
      if (a) {
        /* write in 20 ms slices so an abort takes effect quickly */
        size_t total = len & ~(size_t)3, off = 0;
        while (off < total) {
          if (access(f_abort, F_OK) == 0 || access(f_stop, F_OK) == 0) break;
          size_t n = total - off > 3840 ? 3840 : total - off;
          if (fwrite(a + off, 1, n, stdout) != n) return 1;
          off += n;
        }
        free(a);
      }
      continue;
    }
    if (access(f_end, F_OK) == 0) {
      /* pipe (64 KiB) + ALSA buffer hold ~0.9 s; cover it with silence */
      for (int i = 0; i < 75; i++) fwrite(zero, 1, sizeof zero, stdout);
      fflush(stdout);
      unlink(f_end);
      next = 1;
      int fd = open(f_played, O_WRONLY | O_CREAT | O_TRUNC, 0644);
      if (fd >= 0) close(fd);
      continue;
    }
    if (fwrite(zero, 1, sizeof zero, stdout) != sizeof zero) return 1;
    fflush(stdout);
  }
}

static long mono_ms(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (long)t.tv_sec * 1000L + t.tv_nsec / 1000000;
}

/* Frame level for speech detection, on a pre-emphasised signal
 * (y = x[n] - 0.95 x[n-1]): range hoods, fans and hum are mostly low
 * frequency, speech carries energy higher up, so this keeps speech
 * standing out of steady kitchen noise. */
static float frame_rms(const int16_t *f, size_t frames, int mic) {
  double e = 0;
  for (size_t i = 1; i < frames; i++) {
    double v = f[2 * i + mic] - 0.95 * f[2 * (i - 1) + mic];
    e += v * v;
  }
  float r = (float)(e / frames), x = r > 1 ? r : 1;
  for (int i = 0; i < 20; i++) x = 0.5f * (x + r / x); /* sqrt without libm */
  return x;
}

/* Follow the growing asr_tts .swp recording (44.1 kHz stereo S16) from the
 * wake position and stop when the speaker has finished talking. */
static size_t listen_swp(const char *path, long wake_pos, int16_t **out, float *peak) {
  enum { BPS = 176400, FR = 4410, FRB = FR * 4 }; /* 100 ms frames */
  int fd = open(path, O_RDONLY);
  if (fd < 0) die("open %s: %s", path, strerror(errno));
  int mic = atoi(conf_get("MIC_CHANNEL", "1")) ? 1 : 0;
  int16_t fbuf[FR * 2];
  wake_pos &= ~3L;
  if (wake_pos < 44) wake_pos = 44;

  /* Noise floor: the quietest 100 ms in the 3 s before the wake. */
  float floor = 0;
  for (long p = wake_pos - 3L * BPS; p + FRB <= wake_pos; p += FRB) {
    if (p < 44) continue;
    if (pread(fd, fbuf, FRB, p) != FRB) break;
    float r = frame_rms(fbuf, FR, mic);
    if (floor == 0 || r < floor) floor = r;
  }
  if (floor <= 0) {
    /* No history (fresh recording): take the quietest of the first 0.4 s. */
    long t1 = mono_ms();
    for (long p = wake_pos; p < wake_pos + 4L * FRB && mono_ms() - t1 < 3000;) {
      struct stat st;
      if (fstat(fd, &st) != 0) break;
      if ((long)st.st_size < p + FRB) { usleep(50000); continue; }
      if (pread(fd, fbuf, FRB, p) != FRB) break;
      float r = frame_rms(fbuf, FR, mic);
      if (floor == 0 || r < floor) floor = r;
      p += FRB;
    }
    if (floor <= 0) floor = 40;
  }
  /* Thresholds rise gently with the noise floor (a TV or fan in the room
   * can lift it from ~100 to ~1000). */
  float on = floor * 1.8f + 40, off = floor * 1.3f + 20;
  float maxr = 0;

  long pos = wake_pos, t0 = mono_ms();
  int speech = 0, voiced = 0, silent = 0;
  float speech_peak = 0;
  long onset = -1;
  int strict = getenv("HK_VOICE_STRICT") != NULL; /* follow-ups: no guessing */
  if (strict) on = floor * 2.5f + 60;
  for (;;) {
    struct stat st;
    if (fstat(fd, &st) != 0) break;
    long avail = (long)st.st_size & ~3L;
    int done = 0;
    while (!done && pos + FRB <= avail) {
      if (pread(fd, fbuf, FRB, pos) != FRB) break;
      float r = frame_rms(fbuf, FR, mic);
      pos += FRB;
      if (r > maxr) maxr = r;
      if (speech && r > speech_peak) speech_peak = r;
      if (r >= on && !(speech && r < speech_peak * 0.3f)) {
        silent = 0;
        if (++voiced >= 2 && !speech) {
          speech = 1;
          onset = pos - 3L * FRB; /* the frame before the two voiced ones */
        }
        if (speech && r > speech_peak) speech_peak = r;
      } else {
        voiced = 0;
        /* quiet = back near the room level, or well below the speaker's own
         * level (steady noise hovering at the threshold must not keep us
         * listening) */
        if (speech && (r < off || r < speech_peak * 0.3f)) silent++;
        else if (speech) silent = 0;
      }
      if (speech && silent >= 7) done = 1;          /* 0.7 s of quiet */
      if (pos - wake_pos > 12L * BPS) done = 1;     /* long question */
    }
    if (done) break;
    long el = mono_ms() - t0;
    const char *ns = getenv("HK_VOICE_NOSPEECH_MS");
    if (!speech && el > (ns ? atol(ns) : 6000)) {
      if (strict || maxr < floor * 1.5f || maxr < 60) {
        fprintf(stderr, "hk-voice: no speech after wake (floor %.0f, on %.0f, max %.0f)\n",
                floor, on, maxr);
        close(fd);
        return 0;
      }
      /* Something louder than the room was said; let the recogniser judge. */
      fprintf(stderr, "hk-voice: unsure about speech (floor %.0f, max %.0f); sending anyway\n",
              floor, maxr);
      break;
    }
    if (el > 15000) break;
    usleep(50000);
  }
  /* Find where the question really starts.  People often talk straight
   * after "Alexa", before we even see the wake event, so walk back from the
   * detected onset over voiced frames (up to 1.5 s before the wake) and stop
   * at the first quiet gap of >= 0.2 s, which is the pause after "Alexa". */
  long start = wake_pos - BPS * 3 / 10;
  if (onset >= 0) {
    long cur = onset, limit = wake_pos - BPS * 3 / 2;
    if (limit < 44) limit = 44;
    int quiet = 0;
    while (cur - FRB >= limit) {
      if (pread(fd, fbuf, FRB, cur - FRB) != FRB) break;
      if (frame_rms(fbuf, FR, mic) < off) {
        if (++quiet >= 2) break;
      } else
        quiet = 0;
      cur -= FRB;
    }
    start = cur + (long)quiet * FRB - BPS / 5; /* keep 0.2 s of lead-in */
    if (start < limit) start = limit;
  }
  start &= ~3L;
  if (start < 44) start = 44;
  size_t bytes = (size_t)(pos - start);
  int16_t *raw = malloc(bytes);
  if (!raw) die("out of memory");
  ssize_t got = pread(fd, raw, bytes, start);
  close(fd);
  if (got < 4) die("short read");
  fprintf(stderr, "hk-voice: endpoint after %ld ms, audio %ld ms, floor %.0f\n",
          mono_ms() - t0, (long)got / 176, floor);
  size_t n = mic_convert(raw, (size_t)got / 4, 2, 44100, 0, mic, out, peak);
  free(raw);
  /* keep the last question (16 kHz mono S16) for troubleshooting */
  write_file_atomic("/tmp/hk-voice/last-q.raw", *out, n * 2, 0600);
  return n;
}

static void run_prepare_hook(void) {
  const char *cmd = getenv("HK_VOICE_PREPARE");
  if (!cmd || !*cmd) return;
  pid_t p = fork();
  if (p == 0) {
    execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
    _exit(127);
  }
}

/* ---------- reminders ---------- */
#define REMINDERS "/vendor/hk-voice/reminders"

static int has_any(const char *q, const char **k) {
  for (int i = 0; k[i]; i++)
    if (strstr(q, k[i])) return 1;
  return 0;
}

/* Timer / alarm requests come in many phrasings: either a clear keyword,
 * or a time expression together with a "tell me" kind of verb.  Weather
 * questions ("告诉我明天天气") are handled before the second test. */
static int is_reminder_strong(const char *q) {
  static const char *k[] = {"提醒", "闹钟", "闹铃", "倒计时", "倒数", "定时", "计时", "计个时",
                            "定个时", "叫醒", "掐表", "掐个表", "叫我起", "喊我起", NULL};
  return has_any(q, k);
}

static int is_reminder_question(const char *q) {
  static const char *when[] = {"分钟后", "分钟以后", "小时后", "小时以后", "秒后", "秒以后",
                               "一会", "待会", "等会", "过会", "点钟", "点半", "点叫", "点喊",
                               "明天", "明早", "明晚", "今晚", "早上", "晚上", "下午", "中午",
                               "凌晨", "分钟", "小时", NULL};
  static const char *act[] = {"叫我", "喊我", "叫一下", "喊一下", "告诉我", "通知我", "记得",
                              "别忘", "别让我忘", "起床", "叫醒", NULL};
  return is_reminder_strong(q) || (has_any(q, when) && has_any(q, act));
}

static void beijing_tm(time_t t, struct tm *tm) {
  t += 8 * 3600;
  gmtime_r(&t, tm);
}

/* Handle a reminder request; writes the spoken confirmation to out. */
static void handle_reminder(const char *q, FILE *out) {
  static const char *cancel[] = {"取消", "删除", "删掉", "不用提醒", "清空", NULL};
  static const char *list[] = {"有哪些", "有什么", "几个", "查看", "查一下", NULL};
  if (has_any(q, cancel)) {
    unlink(REMINDERS);
    fprintf(out, "好的，提醒都已经取消了。\n");
    return;
  }
  if (has_any(q, list)) {
    char *r = read_file(REMINDERS, NULL);
    int n = 0;
    long first = 0;
    char ftext[256] = "";
    for (char *l = r ? strtok(r, "\n") : NULL; l; l = strtok(NULL, "\n")) {
      long t = atol(l);
      char *sp = strchr(l, ' ');
      if (!n || t < first) {
        first = t;
        snprintf(ftext, sizeof ftext, "%s", sp ? sp + 1 : "");
      }
      n++;
    }
    free(r);
    if (!n) {
      fprintf(out, "现在没有设置提醒。\n");
      return;
    }
    struct tm tm;
    beijing_tm((time_t)first, &tm);
    fprintf(out, "一共有%d个提醒，最近的是%d点%02d分，%s。\n", n, tm.tm_hour, tm.tm_min,
            *ftext ? ftext : "没有备注");
    return;
  }
  /* Ask the model for the delay and the message. */
  char sys[1200];
  llm_system(sys, sizeof sys, NULL);
  buf_t body = {0}, resp = {0};
  buf_str(&body, "{\"model\":");
  json_escape(&body, conf_get("MODEL", "Qwen/Qwen3.8-Flash-Next"));
  buf_str(&body, ",\"stream\":false,\"enable_thinking\":false,\"max_tokens\":120,"
                 "\"tool_choice\":\"required\",\"messages\":[{\"role\":\"system\",\"content\":");
  char extra[1500];
  snprintf(extra, sizeof extra,
           "%s 用户要设置提醒。相对时间（如“十分钟后”）填 seconds；"
           "具体时刻（如“明早七点”）填 day（0今天 1明天 2后天）和 time（24小时制 HH:MM，北京时间），"
           "不要自己换算成秒。并提炼要提醒的事情，没有说事情就留空。",
           sys);
  json_escape(&body, extra);
  buf_str(&body, "},{\"role\":\"user\",\"content\":");
  json_escape(&body, q);
  buf_str(&body, "}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"set_reminder\","
                 "\"description\":\"设置一个提醒\",\"parameters\":{\"type\":\"object\","
                 "\"properties\":{\"seconds\":{\"type\":\"integer\",\"description\":\"相对时间：多少秒后\"},"
                 "\"day\":{\"type\":\"integer\",\"description\":\"具体时刻的日期：0今天1明天2后天\"},"
                 "\"time\":{\"type\":\"string\",\"description\":\"具体时刻 HH:MM（24小时制）\"},"
                 "\"text\":{\"type\":\"string\",\"description\":\"要提醒的事情，简短\"}}}}}]}");
  char auth[600];
  llm_auth(auth, sizeof auth);
  const char *hdr[] = {"Content-Type: application/json", auth, NULL};
  long code = http_post(llm_url(), hdr, body.p, body.n, &resp, NULL, 0, 20);
  memset(auth, 0, sizeof auth);
  free(body.p);
  char *args = code == 200 ? json_find_str(resp.p, "arguments", NULL) : NULL;
  free(resp.p);
  long secs = args ? json_find_num(args, "seconds") : -1;
  char *text = args ? json_find_str(args, "text", NULL) : NULL;
  char *at = args ? json_find_str(args, "time", NULL) : NULL;
  int at_given = at && *at;
  if (at && *at) { /* absolute Beijing time: compute the delay ourselves */
    int hh = -1, mm = 0;
    if (sscanf(at, "%d:%d", &hh, &mm) >= 1 && hh >= 0 && hh < 24 && mm >= 0 && mm < 60) {
      long day = json_find_num(args, "day");
      if (day < 0 || day > 6) day = 0;
      time_t now = time(NULL);
      long bj = (long)now + 8 * 3600;                 /* seconds in Beijing */
      long midnight = bj - bj % 86400;
      long target = midnight + day * 86400 + hh * 3600L + mm * 60L;
      if (day == 0 && target <= bj) target += 86400;  /* "七点" already passed: tomorrow */
      secs = target - bj;
    }
  }
  free(at);
  free(args);
  /* A timer ("倒计时/计时/掐表") of half a day or more is almost certainly a
   * misrecognition ("两分钟" heard as "两天"): read it back, don't set it. */
  static const char *timer_words[] = {"倒计时", "倒数", "计时", "计个时", "掐表", "掐个表",
                                      "定时", "定个时", NULL};
  if (secs >= 12 * 3600 && has_any(q, timer_words) && !(at_given)) {
    fprintf(out, "我听到的是：%s 时间有点长，没有设置，如果不对请再说一遍。\n", q);
    fprintf(stderr, "hk-voice: timer of %ld s looks misheard; not set\n", secs);
    free(text);
    return;
  }
  if (secs < 5 || secs > 7L * 86400) {
    fprintf(out, "%s\n", code == 200 ? "抱歉，没听明白要什么时候提醒你，请再说一遍。"
                                     : "抱歉，网络有点问题，提醒没有设置成功。");
    free(text);
    return;
  }
  char t2[200] = "";
  if (text) {
    snprintf(t2, sizeof t2, "%s", text);
    for (char *p = t2; *p; p++)
      if (*p == '\n' || *p == '\r') *p = ' ';
  }
  free(text);
  time_t due = time(NULL) + secs;
  FILE *f = fopen(REMINDERS, "a");
  if (f) {
    fprintf(f, "%ld %s\n", (long)due, t2);
    fclose(f);
  }
  if (secs < 60)
    fprintf(out, "好的，%ld秒后提醒你%s%s。\n", secs, *t2 ? "：" : "", t2);
  else if (secs < 3600)
    fprintf(out, "好的，%ld分钟后提醒你%s%s。\n", (secs + 30) / 60, *t2 ? "：" : "", t2);
  else {
    struct tm now_tm, due_tm;
    beijing_tm(time(NULL), &now_tm);
    beijing_tm(due, &due_tm);
    int dd = (due_tm.tm_year - now_tm.tm_year) * 400 + due_tm.tm_yday - now_tm.tm_yday;
    const char *day = dd == 0 ? "今天" : dd == 1 ? "明天" : dd == 2 ? "后天" : "";
    char daybuf[32];
    if (!*day) snprintf(daybuf, sizeof daybuf, "%d月%d日", due_tm.tm_mon + 1, due_tm.tm_mday), day = daybuf;
    fprintf(out, "好的，%s%d点%02d分提醒你%s%s。\n", day, due_tm.tm_hour, due_tm.tm_min,
            *t2 ? "：" : "", t2);
  }
  fprintf(stderr, "hk-voice: reminder in %ld s: %s\n", secs, t2);
}

/* Print reminders that are due (one text per line) and remove them. */
static int reminders_due(void) {
  char *r = read_file(REMINDERS, NULL);
  if (!r) return 0;
  time_t now = time(NULL);
  buf_t keep = {0};
  buf_add(&keep, "", 0);
  int n = 0;
  for (char *l = strtok(r, "\n"); l; l = strtok(NULL, "\n")) {
    long t = atol(l);
    char *sp = strchr(l, ' ');
    if (t > now) {
      buf_str(&keep, l);
      buf_str(&keep, "\n");
    } else if (now - t < 1800) { /* missed by more than 30 min: drop */
      printf("%s\n", sp && sp[1] ? sp + 1 : "-");
      n++;
    }
  }
  free(r);
  if (keep.n) write_file_atomic(REMINDERS, keep.p, keep.n, 0644);
  else unlink(REMINDERS);
  free(keep.p);
  return n;
}

static int answer_sequential(const char *q, const char *qdir) {
  long t0 = mono_ms();
  char *a = NULL;
  if (is_reminder_strong(q) || (!is_weather_question(q) && is_reminder_question(q))) {
    size_t an = 0;
    FILE *out = open_memstream(&a, &an);
    if (out) {
      handle_reminder(q, out);
      fclose(out);
    }
    if (a) {
      while (an && (a[an - 1] == '\n' || a[an - 1] == '\r')) a[--an] = 0;
    }
  } else {
    char *ctx = NULL;
    if (is_weather_question(q)) {
      char city[128];
      int day;
      weather_args(q, city, sizeof city, &day);
      ctx = weather_report(city, day);
      fprintf(stderr, "hk-voice: weather %s day %d: %s (%ld ms)\n", city, day,
              ctx ? ctx : "unavailable", mono_ms() - t0);
      if (!ctx) ctx = strdup("天气服务暂时查不到数据。");
      /* The forecast is complete in memory; do not retain its TLS session
       * while the model prepares the reply. */
      http_close();
    }
    a = llm_ask_ctx(q, ctx, NULL);
    free(ctx);
  }
  /* Release the model/reminder TLS session before creating Baidu TTS one. */
  http_close();
  if (!a) a = strdup("抱歉，我暂时连不上大模型，请稍后再试。");
  speakable(a, 900);
  printf("A: %s\n", a);
  fflush(stdout);

  float g = output_gain_db();
  char tmp[300], fin[300];
  int nq = 0;
  snprintf(tmp, sizeof tmp, "%s/q.tmp", qdir);
  if (tts_to_file(a, tmp, g) == 0) {
    snprintf(fin, sizeof fin, "%s/q.1.raw", qdir);
    if (rename(tmp, fin) == 0) nq = 1;
  }
  if (nq == 0) {
    snprintf(tmp, sizeof tmp, "%s/q.tmp", qdir);
    if (clip_to_file(tts_svc_err && !net_err ? "ttserr" : "neterr", tmp, g) == 0) {
      snprintf(fin, sizeof fin, "%s/q.1.raw", qdir);
      if (rename(tmp, fin) == 0) nq = 1;
    }
  }
  snprintf(fin, sizeof fin, "%s/q.end", qdir);
  int fd = open(fin, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (fd >= 0) close(fd);
  fprintf(stderr, "hk-voice: sequential reply done, %d sentences, %ld ms\n",
          nq, mono_ms() - t0);
  free(a);
  return nq ? 0 : 1;
}

/* Answer q: on a roomy host, the model streams sentences from a child process
 * while this process synthesises each one into <qdir>/q.N.raw. The Allure is
 * memory constrained; below the kernel's 64 MiB reclaimable-memory trigger,
 * use the single-process path so the LLM and TTS peaks cannot overlap. */
static int answer_streamed(const char *q, const char *qdir) {
  if (sequential_reply()) {
    long available = mem_available_kib();
    http_close();
    fprintf(stderr, "hk-voice: sequential reply mode (available %ld KiB)\n", available);
    return answer_sequential(q, qdir);
  }
  int pfd[2];
  if (pipe(pfd) != 0) die("pipe");
  long t0 = mono_ms();
  pid_t pid = fork();
  if (pid == 0) {
    close(pfd[0]);
    FILE *out = fdopen(pfd[1], "w");
    char *ctx = NULL;
    if (is_reminder_strong(q) || (!is_weather_question(q) && is_reminder_question(q))) {
      handle_reminder(q, out);
      fclose(out);
      _exit(0);
    }
    if (is_weather_question(q)) {
      char city[128];
      int day;
      weather_args(q, city, sizeof city, &day);
      ctx = weather_report(city, day);
      fprintf(stderr, "hk-voice: weather %s day %d: %s (%ld ms)\n", city, day,
              ctx ? ctx : "unavailable", mono_ms() - t0);
      if (!ctx) ctx = strdup("天气服务暂时查不到数据。");
    }
    char *a = llm_ask_ctx(q, ctx, out);
    if (!a) fprintf(out, "抱歉，我暂时连不上大模型，请稍后再试。\n");
    else printf("A: %s\n", a);
    fflush(stdout);
    fclose(out);
    _exit(0);
  }
  close(pfd[1]);
  FILE *in = fdopen(pfd[0], "r");
  float g = output_gain_db();
  { /* open the TLS connection to the TTS host while the model thinks */
    buf_t w = {0};
    http_post("https://tsn.baidu.com/", NULL, NULL, 0, &w, NULL, 0, 4);
    free(w.p);
    net_err = 0;
  }
  char line[2048], tmp[300], fin[300];
  int nq = 0;
  while (fgets(line, sizeof line, in)) {
    line[strcspn(line, "\n")] = 0;
    if (!*line) continue;
    snprintf(tmp, sizeof tmp, "%s/q.tmp", qdir);
    if (tts_to_file(line, tmp, g) != 0) continue;
    snprintf(fin, sizeof fin, "%s/q.%d.raw", qdir, ++nq);
    rename(tmp, fin);
    if (nq == 1) fprintf(stderr, "hk-voice: first sentence ready after %ld ms (gain %.1f dB)\n", mono_ms() - t0, g);
  }
  fclose(in);
  waitpid(pid, NULL, 0);
  if (nq == 0) { /* network trouble: say so with a stored prompt */
    snprintf(tmp, sizeof tmp, "%s/q.tmp", qdir);
    if (clip_to_file(tts_svc_err && !net_err ? "ttserr" : "neterr", tmp, g) == 0) {
      snprintf(fin, sizeof fin, "%s/q.%d.raw", qdir, ++nq);
      rename(tmp, fin);
      fprintf(stderr, "hk-voice: no reply synthesised; played the network prompt\n");
    }
  }
  snprintf(fin, sizeof fin, "%s/q.end", qdir);
  int fd = open(fin, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (fd >= 0) close(fd);
  fprintf(stderr, "hk-voice: reply done, %d sentences, %ld ms\n", nq, mono_ms() - t0);
  return nq ? 0 : 1;
}

int main(int argc, char **argv) {
  setvbuf(stdout, NULL, _IOLBF, 0);
  if (argc >= 3 && !strcmp(argv[1], "ask")) {
    char *a = llm_ask(argv[2]);
    if (!a) return 1;
    speakable(a, 900);
    puts(a);
    return 0;
  }
  if (argc >= 5 && !strcmp(argv[1], "feed")) return feed(argv[2], argv[3], argv[4]);
  if (argc >= 3 && !strcmp(argv[1], "feedq")) return feedq(argv[2]);
  if (argc >= 2 && !strcmp(argv[1], "due")) return reminders_due() ? 0 : 1;
  if (argc >= 3 && !strcmp(argv[1], "classify")) { /* classify <text>... */
    for (int i = 2; i < argc; i++) {
      const char *q = argv[i];
      const char *k = local_action(q) ? "command"
                      : (is_reminder_strong(q) || (!is_weather_question(q) && is_reminder_question(q)))
                          ? "reminder"
                      : is_weather_question(q) ? "weather" : "chat";
      printf("%-8s %s\n", k, q);
    }
    return 0;
  }
  if (argc >= 2 && !strcmp(argv[1], "mkclips")) return make_clips();
  if (argc >= 2 && !strcmp(argv[1], "check")) { /* verify the configured keys */
    int bad = 0;
    unlink(TOKEN_PATH);
    const char *t = baidu_token(1);
    printf("baidu token: %s\n", t ? "ok" : "FAILED");
    buf_t w = {0};
    int tts = t && baidu_tts("测试", &w) == 0;
    free(w.p);
    printf("baidu tts: %s\n", tts ? "ok" : "FAILED");
    char *a = llm_ask("只回复两个字：正常");
    int llm = a != NULL;
    printf("modelscope: %s\n", llm ? "ok" : "FAILED");
    free(a);
    unlink(HIST_PATH);
    bad = !t || !tts || !llm;
    return bad;
  }
  if (argc >= 4 && !strcmp(argv[1], "clip")) /* clip <name> <out.raw> */
    return clip_to_file(argv[2], argv[3], output_gain_db()) ? 1 : 0;
  if (argc >= 3 && !strcmp(argv[1], "asr16")) { /* asr16 <16k mono raw> */
    size_t len;
    char *d = read_file(argv[2], &len);
    if (!d) return 1;
    long t = mono_ms();
    char *q = asr_best((int16_t *)d, len / 2);
    printf("%s (%ld ms)\n", q ? q : "(none)", mono_ms() - t);
    return 0;
  }
  if (argc >= 4 && !strcmp(argv[1], "nr")) { /* nr <in 16k mono raw> <out> */
    size_t len;
    char *d = read_file(argv[2], &len);
    if (!d) return 1;
    denoise((int16_t *)d, len / 2);
    return write_file_atomic(argv[3], d, len & ~(size_t)1, 0644) ? 1 : 0;
  }
  if (argc >= 3 && !strcmp(argv[1], "tone")) return write_tone(argv[2], output_gain_db() - 6) ? 1 : 0;
  if (argc >= 2 && !strcmp(argv[1], "gain")) {
    printf("%.1f\n", output_gain_db());
    return 0;
  }
  if (argc >= 3 && !strcmp(argv[1], "answer") && getenv("HK_VOICE_QDIR"))
    return answer_streamed(argv[2], getenv("HK_VOICE_QDIR"));
  if (argc >= 2 && !strcmp(argv[1], "weather-refresh")) {
    char *j = forecast_json(default_city(), 1);
    free(j);
    return j ? 0 : 1;
  }
  if (argc >= 3 && !strcmp(argv[1], "weather")) {
    char city[128];
    int day;
    weather_args(argv[2], city, sizeof city, &day);
    char *w = weather_report(city, day);
    printf("%s %d: %s\n", city, day, w ? w : "(none)");
    return 0;
  }
  if (argc >= 4 && !strcmp(argv[1], "say")) return say_to(argv[2], argv[3]) ? 1 : 0;
  if ((argc >= 3 && (!strcmp(argv[1], "asr") || !strcmp(argv[1], "run"))) ||
      (argc >= 5 && !strcmp(argv[1], "listen"))) {
    int16_t *pcm;
    float peak;
    long t0 = mono_ms();
    size_t n;
    if (!strcmp(argv[1], "listen")) {
      n = listen_swp(argv[2], atol(argv[3]), &pcm, &peak);
      if (n == 0) return 4; /* nothing said; no prepare started */
      run_prepare_hook(); /* open the speaker path while we think */
      argv[3] = argv[4];
      argv[1] = "run";
    } else {
      n = load_mic(argv[2], &pcm, &peak);
    }
    fprintf(stderr, "hk-voice: mic %zu ms, raw peak %.0f\n", n / 16, peak);
    if (n < 16000 / 2 || peak < 60) {
      fprintf(stderr, "hk-voice: too short or silent\n");
      return 3;
    }
    if (strcmp(conf_get("DENOISE", "0"), "0")) {
      long td = mono_ms();
      denoise(pcm, n);
      fprintf(stderr, "hk-voice: denoise %ld ms\n", mono_ms() - td);
      write_file_atomic("/tmp/hk-voice/last-q-nr.raw", pcm, n * 2, 0600);
    }
    char *q = asr_best(pcm, n);
    free(pcm);
    /* Drop the ASR connection before LLM/TTS starts its own TLS session. */
    http_close();
    if (!q || !*q) {
      const char *qd = getenv("HK_VOICE_QDIR");
      if (qd && net_err) {
        fprintf(stderr, "hk-voice: network down; playing the prompt\n");
        queue_reply(qd, NULL, "neterr");
        return 7; /* spoken, no follow-up */
      }
      if (qd && asr_svc_err) {
        fprintf(stderr, "hk-voice: baidu recognition service error; playing the prompt\n");
        queue_reply(qd, NULL, "asrerr");
        return 7;
      }
      if (qd && !getenv("HK_VOICE_STRICT") && peak >= 1000) {
        fprintf(stderr, "hk-voice: nothing recognised; asking again\n");
        queue_reply(qd, "没听清，请再说一遍。", "unclear");
        return 0; /* spoken; the follow-up window lets them repeat */
      }
      fprintf(stderr, "hk-voice: nothing recognised\n");
      return 3;
    }
    printf("Q: %s (asr %ld ms)\n", q, mono_ms() - t0);
    t0 = mono_ms();
    if (!strcmp(argv[1], "asr")) return 0;
    const char *act = local_action(q);
    if (act) {
      const char *af = getenv("HK_VOICE_ACTION_FILE");
      printf("ACTION: %s\n", act);
      if (af) write_file_atomic(af, act, strlen(act), 0644);
      return 5;
    }
    const char *qdir = getenv("HK_VOICE_QDIR");
    if (qdir) return answer_streamed(q, qdir);
    if (argc < 4) die("run needs an output path");
    char *a = llm_ask(q);
    if (!a) a = strdup("抱歉，我暂时连不上大模型，请稍后再试。");
    speakable(a, 900);
    printf("A: %s (llm %ld ms)\n", a, mono_ms() - t0);
    t0 = mono_ms();
    int rc = say_to(a, argv[3]);
    fprintf(stderr, "hk-voice: tts done in %ld ms\n", mono_ms() - t0);
    return rc ? 1 : 0;
  }
  fprintf(stderr,
          "usage: hk-voice run <in.wav> <out.raw> | asr <in.wav> | ask <text> | say <text> <out.raw>\n");
  return 2;
}
