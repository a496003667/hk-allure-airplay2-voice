/*
 * The AP2 glibc 2.27 runtime cannot load the device's glibc 2.19 NSS
 * modules. D-Bus needs root, while Avahi needs nobody. Supply only those
 * fixed service records so the isolated runtime never touches the system
 * account databases.
 */

typedef unsigned int uid_t;
typedef unsigned int gid_t;
typedef __SIZE_TYPE__ size_t;

#define NULL ((void *)0)
#define ERANGE 34

struct passwd {
    char *pw_name;
    char *pw_passwd;
    uid_t pw_uid;
    gid_t pw_gid;
    char *pw_gecos;
    char *pw_dir;
    char *pw_shell;
};

struct group {
    char *gr_name;
    char *gr_passwd;
    gid_t gr_gid;
    char **gr_mem;
};

static char root_name[] = "root";
static char root_password[] = "x";
static char root_gecos[] = "root";
static char root_dir[] = "/root";
static char root_shell[] = "/bin/sh";
static struct passwd root_user = {
    root_name,
    root_password,
    0,
    0,
    root_gecos,
    root_dir,
    root_shell,
};

static char nobody_name[] = "nobody";
static char nobody_password[] = "x";
static char nobody_gecos[] = "nobody";
static char nobody_dir[] = "/home";
static char nobody_shell[] = "/bin/false";
static struct passwd nobody_user = {
    nobody_name,
    nobody_password,
    99,
    99,
    nobody_gecos,
    nobody_dir,
    nobody_shell,
};

static char root_group_name[] = "root";
static char nobody_group_name[] = "nogroup";
static char group_password[] = "x";
static char *no_group_members[] = {NULL};
static struct group root_group = {
    root_group_name,
    group_password,
    0,
    no_group_members,
};
static struct group nobody_group = {
    nobody_group_name,
    group_password,
    99,
    no_group_members,
};

static int string_equals(const char *left, const char *right) {
    while (*left != '\0' && *left == *right) {
        ++left;
        ++right;
    }
    return *left == *right;
}

static int copy_string(char **destination, const char *source,
                       char **cursor, char *end) {
    size_t length = 0;
    while (source[length] != '\0') {
        ++length;
    }
    if (*cursor + length + 1 > end) {
        return ERANGE;
    }

    *destination = *cursor;
    for (size_t index = 0; index <= length; ++index) {
        (*cursor)[index] = source[index];
    }
    *cursor += length + 1;
    return 0;
}

static int copy_user(struct passwd *destination, const struct passwd *source,
                     char *buffer, size_t buffer_length,
                     struct passwd **result) {
    char *cursor = buffer;
    char *end;
    int status;

    *result = NULL;
    if (buffer == NULL || buffer_length == 0) {
        return ERANGE;
    }
    end = buffer + buffer_length;

    status = copy_string(&destination->pw_name, source->pw_name,
                         &cursor, end);
    if (status == 0)
        status = copy_string(&destination->pw_passwd, source->pw_passwd,
                             &cursor, end);
    if (status == 0)
        status = copy_string(&destination->pw_gecos, source->pw_gecos,
                             &cursor, end);
    if (status == 0)
        status = copy_string(&destination->pw_dir, source->pw_dir,
                             &cursor, end);
    if (status == 0)
        status = copy_string(&destination->pw_shell, source->pw_shell,
                             &cursor, end);
    if (status != 0) {
        return status;
    }

    destination->pw_uid = source->pw_uid;
    destination->pw_gid = source->pw_gid;
    *result = destination;
    return 0;
}

static int copy_group(struct group *destination, const struct group *source,
                      char *buffer, size_t buffer_length,
                      struct group **result) {
    char *cursor = buffer;
    char *end;
    int status;

    *result = NULL;
    if (buffer == NULL || buffer_length == 0) {
        return ERANGE;
    }
    end = buffer + buffer_length;

    status = copy_string(&destination->gr_name, source->gr_name,
                         &cursor, end);
    if (status == 0)
        status = copy_string(&destination->gr_passwd, source->gr_passwd,
                             &cursor, end);
    if (status != 0) {
        return status;
    }

    while (((unsigned long)cursor & (sizeof(char *) - 1)) != 0) {
        if (cursor >= end) {
            return ERANGE;
        }
        *cursor++ = '\0';
    }
    if (cursor + sizeof(char *) > end) {
        return ERANGE;
    }
    destination->gr_gid = source->gr_gid;
    destination->gr_mem = (char **)cursor;
    destination->gr_mem[0] = NULL;
    *result = destination;
    return 0;
}

static struct passwd *user_by_name(const char *name) {
    if (name == NULL) {
        return NULL;
    }
    if (string_equals(name, root_name)) {
        return &root_user;
    }
    if (string_equals(name, nobody_name)) {
        return &nobody_user;
    }
    return NULL;
}

static struct passwd *user_by_uid(uid_t uid) {
    if (uid == 0) {
        return &root_user;
    }
    if (uid == 99) {
        return &nobody_user;
    }
    return NULL;
}

static struct group *group_by_name(const char *name) {
    if (name == NULL) {
        return NULL;
    }
    if (string_equals(name, root_group_name)) {
        return &root_group;
    }
    if (string_equals(name, nobody_group_name)) {
        return &nobody_group;
    }
    return NULL;
}

static struct group *group_by_gid(gid_t gid) {
    if (gid == 0) {
        return &root_group;
    }
    if (gid == 99) {
        return &nobody_group;
    }
    return NULL;
}

struct passwd *getpwnam(const char *name) {
    return user_by_name(name);
}

struct passwd *getpwuid(uid_t uid) {
    return user_by_uid(uid);
}

int getpwnam_r(const char *name, struct passwd *user, char *buffer,
               size_t buffer_length, struct passwd **result) {
    struct passwd *source = user_by_name(name);
    if (source == NULL) {
        *result = NULL;
        return 0;
    }
    return copy_user(user, source, buffer, buffer_length, result);
}

int getpwuid_r(uid_t uid, struct passwd *user, char *buffer,
               size_t buffer_length, struct passwd **result) {
    struct passwd *source = user_by_uid(uid);
    if (source == NULL) {
        *result = NULL;
        return 0;
    }
    return copy_user(user, source, buffer, buffer_length, result);
}

struct group *getgrnam(const char *name) {
    return group_by_name(name);
}

struct group *getgrgid(gid_t gid) {
    return group_by_gid(gid);
}

int getgrnam_r(const char *name, struct group *group, char *buffer,
               size_t buffer_length, struct group **result) {
    struct group *source = group_by_name(name);
    if (source == NULL) {
        *result = NULL;
        return 0;
    }
    return copy_group(group, source, buffer, buffer_length, result);
}

int getgrgid_r(gid_t gid, struct group *group, char *buffer,
               size_t buffer_length, struct group **result) {
    struct group *source = group_by_gid(gid);
    if (source == NULL) {
        *result = NULL;
        return 0;
    }
    return copy_group(group, source, buffer, buffer_length, result);
}

int getgrouplist(const char *name, gid_t base_group, gid_t *groups,
                 int *group_count) {
    int needed = 1;

    if (user_by_name(name) == NULL) {
        *group_count = 0;
        return -1;
    }
    if (groups == NULL || *group_count < needed) {
        *group_count = needed;
        return -1;
    }

    groups[0] = base_group;
    *group_count = needed;
    return needed;
}
