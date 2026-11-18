#define _GNU_SOURCE

#include <dlfcn.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

typedef void *plist_t;

typedef void (*plist_set_item_fn)(plist_t node, const char *key, plist_t item);
typedef plist_t (*plist_get_item_fn)(plist_t node, const char *key);
typedef plist_t (*plist_new_real_fn)(double value);
typedef plist_t (*plist_new_uint_fn)(uint64_t value);
typedef plist_t (*plist_new_data_fn)(const char *value, uint64_t length);
typedef void (*plist_get_data_val_fn)(plist_t node, char **value,
                                      uint64_t *length);
typedef void (*plist_mem_free_fn)(void *value);
typedef void *(*avahi_string_list_new_from_array_fn)(const char *const *array,
                                                     int length);

extern int unsetenv(const char *name);

static plist_set_item_fn real_set_item;
static plist_get_item_fn real_get_item;
static plist_new_real_fn real_new_real;
static plist_new_uint_fn real_new_uint;
static plist_new_data_fn real_new_data;
static plist_get_data_val_fn real_get_data_val;
static plist_mem_free_fn real_mem_free;
static avahi_string_list_new_from_array_fn real_avahi_string_list_new_from_array;
static int logged_once;
static int txt_airplay_vv_logged;
static int mdns_vv_logged;

/*
 * The shim is preloaded only for Shairport.  Do not leak that preload into
 * hard-float helper executables launched by the playback and volume hooks.
 */
static void clear_preload_environment(void) __attribute__((constructor));

static void clear_preload_environment(void) {
  unsetenv("LD_PRELOAD");
}

static void resolve_set_item(void) {
  if (real_set_item == NULL) {
    real_set_item = (plist_set_item_fn)dlsym(RTLD_NEXT, "plist_dict_set_item");
  }
}

static int resolve_info_helpers(void) {
  if (real_get_item == NULL) {
    real_get_item = (plist_get_item_fn)dlsym(RTLD_NEXT, "plist_dict_get_item");
  }
  if (real_new_real == NULL) {
    real_new_real = (plist_new_real_fn)dlsym(RTLD_NEXT, "plist_new_real");
  }
  if (real_new_uint == NULL) {
    real_new_uint = (plist_new_uint_fn)dlsym(RTLD_NEXT, "plist_new_uint");
  }
  return real_get_item != NULL && real_new_real != NULL &&
         real_new_uint != NULL;
}

static int resolve_data_helpers(void) {
  if (real_new_data == NULL) {
    real_new_data = (plist_new_data_fn)dlsym(RTLD_NEXT, "plist_new_data");
  }
  if (real_get_data_val == NULL) {
    real_get_data_val =
        (plist_get_data_val_fn)dlsym(RTLD_NEXT, "plist_get_data_val");
  }
  if (real_mem_free == NULL) {
    real_mem_free = (plist_mem_free_fn)dlsym(RTLD_NEXT, "plist_mem_free");
  }
  return real_new_data != NULL && real_get_data_val != NULL &&
         real_mem_free != NULL;
}

static int pstring_has_vv(const unsigned char *data, size_t length) {
  size_t offset = 0;

  while (offset < length) {
    size_t field_length = data[offset++];
    if (field_length > length - offset) {
      return 0;
    }
    if (field_length >= 3 && data[offset] == 'v' &&
        data[offset + 1] == 'v' && data[offset + 2] == '=') {
      return 1;
    }
    offset += field_length;
  }
  return 0;
}

/*
 * Shairport 4.3.6 builds Stage 1 /info's txtAirPlay value as a list of
 * length-prefixed strings.  Current Apple senders also inspect this copy of
 * the TXT record, not only the Stage 2 plist, when deciding whether a
 * receiver has a controllable volume.  Append the standard vv field while
 * preserving the rest of the binary data byte-for-byte.
 */
static plist_t add_vv_to_txt_airplay(plist_t item) {
  static const unsigned char vv_field[] = {4, 'v', 'v', '=', '2'};
  unsigned char buffer[4096];
  char *data = NULL;
  uint64_t data_length = 0;
  uint64_t i;
  plist_t replacement;

  if (!resolve_data_helpers()) {
    return item;
  }

  real_get_data_val(item, &data, &data_length);
  if (data == NULL) {
    return item;
  }

  if (data_length > sizeof(buffer) - sizeof(vv_field) ||
      pstring_has_vv((const unsigned char *)data, (size_t)data_length)) {
    real_mem_free(data);
    return item;
  }

  for (i = 0; i < data_length; i++) {
    buffer[i] = (unsigned char)data[i];
  }
  for (i = 0; i < sizeof(vv_field); i++) {
    buffer[data_length + i] = vv_field[i];
  }

  replacement = real_new_data((const char *)buffer,
                              data_length + sizeof(vv_field));
  real_mem_free(data);
  if (replacement == NULL) {
    return item;
  }

  if (!txt_airplay_vv_logged) {
    static const char message[] =
        "hk-ap2-info-shim: added vv=2 to Stage 1 txtAirPlay\n";
    write(STDERR_FILENO, message, sizeof(message) - 1);
    txt_airplay_vv_logged = 1;
  }
  return replacement;
}

static int starts_with(const char *value, const char *prefix) {
  while (*prefix != '\0') {
    if (*value++ != *prefix++) {
      return 0;
    }
  }
  return 1;
}

static int avahi_records_are_airplay2(const char *const *records) {
  int i;
  for (i = 0; i < 64 && records[i] != NULL; i++) {
    if (starts_with(records[i], "pi=")) {
      return 1;
    }
  }
  return 0;
}

static int avahi_records_have_vv(const char *const *records) {
  int i;
  for (i = 0; i < 64 && records[i] != NULL; i++) {
    if (starts_with(records[i], "vv=")) {
      return 1;
    }
  }
  return 0;
}

/*
 * The AirPlay 2 _airplay._tcp advertisement is built from a second Avahi
 * string list.  Add vv there as well so senders see the same capability
 * before and after the RTSP /info exchange.
 */
void *avahi_string_list_new_from_array(const char *const *records, int length) {
  const char *with_vv[65];
  int count = 0;

  if (real_avahi_string_list_new_from_array == NULL) {
    real_avahi_string_list_new_from_array =
        (avahi_string_list_new_from_array_fn)dlsym(
            RTLD_NEXT, "avahi_string_list_new_from_array");
  }
  if (real_avahi_string_list_new_from_array == NULL || records == NULL) {
    return real_avahi_string_list_new_from_array != NULL
               ? real_avahi_string_list_new_from_array(records, length)
               : NULL;
  }

  if (!avahi_records_are_airplay2(records) ||
      avahi_records_have_vv(records)) {
    return real_avahi_string_list_new_from_array(records, length);
  }

  while (count < 64 && records[count] != NULL) {
    with_vv[count] = records[count];
    count++;
  }
  with_vv[count++] = "vv=2";
  with_vv[count] = NULL;

  if (!mdns_vv_logged) {
    static const char message[] =
        "hk-ap2-info-shim: added vv=2 to AirPlay 2 mDNS TXT\n";
    write(STDERR_FILENO, message, sizeof(message) - 1);
    mdns_vv_logged = 1;
  }
  return real_avahi_string_list_new_from_array(with_vv, -1);
}

static int is_info_response(plist_t node) {
  return real_get_item(node, "features") != NULL &&
         real_get_item(node, "statusFlags") != NULL &&
         real_get_item(node, "deviceID") != NULL &&
         real_get_item(node, "pi") != NULL;
}

/* 5.6 owns its own stable device identity; never override random UUIDs
 * with a hard-coded speaker address. */

void plist_dict_set_item(plist_t node, const char *key, plist_t item) {
  resolve_set_item();
  if (real_set_item == NULL) {
    return;
  }

  if (strcmp(key, "txtAirPlay") == 0) {
    item = add_vv_to_txt_airplay(item);
  }

  real_set_item(node, key, item);

  /*
   * Shairport Sync 4.3.6 builds the AirPlay /info response through this
   * function but omits the three fields current Apple senders use to expose
   * receiver volume.  The stock 4.3.6 SET_PARAMETER volume handler remains
   * intact and is used once the sender advertises a controllable volume.
   */
  if (strcmp(key, "name") == 0 && resolve_info_helpers() &&
      is_info_response(node)) {
    real_set_item(node, "initialVolume", real_new_real(-18.0));
    /*
     * Expose vv=2 in the discovery record, Stage 1 txtAirPlay and Stage 2
     * plist.  Current Apple senders require the same capability value in all
     * three locations before they treat the receiver volume as controllable.
     */
    real_set_item(node, "vv", real_new_uint(2));
    real_set_item(node, "volumeControlType", real_new_uint(3));

    if (!logged_once) {
      static const char message[] =
          "hk-ap2-info-shim: added initialVolume, vv and volumeControlType\n";
      write(STDERR_FILENO, message, sizeof(message) - 1);
      logged_once = 1;
    }
  }
}
