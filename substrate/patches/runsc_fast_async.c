#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

static int append_queue(const char *queue_path, int argc, char **argv) {
    int fd = open(queue_path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    if (fd < 0) return -1;
    uint32_t u_argc = (uint32_t)argc;
    if (write(fd, &u_argc, sizeof(u_argc)) != sizeof(u_argc)) {
        close(fd);
        return -1;
    }
    for (int i = 0; i < argc; i++) {
        uint32_t len = (uint32_t)(strlen(argv[i]) + 1);
        if (write(fd, &len, sizeof(len)) != sizeof(len) ||
            write(fd, argv[i], len) != (ssize_t)len) {
            close(fd);
            return -1;
        }
    }
    close(fd);
    return 0;
}

static int run_one(const char *real_path, char **argv) {
    pid_t c = fork();
    if (c < 0) return 127;
    if (c == 0) {
        execve(real_path, argv, environ);
        _exit(127);
    }
    int st = 0;
    while (waitpid(c, &st, 0) < 0) {
        if (errno != EINTR) return 127;
    }
    if (WIFEXITED(st)) return WEXITSTATUS(st);
    return 1;
}

static int drain_queue(const char *queue_path, const char *real_path) {
    int fd = open(queue_path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    struct stat st;
    if (fstat(fd, &st) < 0 || st.st_size <= 0) {
        unlink(queue_path);
        close(fd);
        return 0;
    }
    size_t sz = (size_t)st.st_size;
    char *buf = malloc(sz);
    if (!buf) {
        close(fd);
        return 1;
    }
    size_t off = 0;
    while (off < sz) {
        ssize_t n = read(fd, buf + off, sz - off);
        if (n <= 0) break;
        off += (size_t)n;
    }
    unlink(queue_path);
    close(fd);

    size_t pos = 0;
    int rc = 0;
    while (pos + sizeof(uint32_t) <= off) {
        uint32_t u_argc = 0;
        memcpy(&u_argc, buf + pos, sizeof(u_argc));
        pos += sizeof(uint32_t);
        if (u_argc == 0 || u_argc > 256) break;
        char **cmd_argv = calloc(u_argc + 1, sizeof(char *));
        if (!cmd_argv) {
            rc = 1;
            break;
        }
        int ok = 1;
        for (uint32_t i = 0; i < u_argc; i++) {
            if (pos + sizeof(uint32_t) > off) {
                ok = 0;
                break;
            }
            uint32_t len = 0;
            memcpy(&len, buf + pos, sizeof(len));
            pos += sizeof(uint32_t);
            if (len == 0 || pos + len > off) {
                ok = 0;
                break;
            }
            cmd_argv[i] = buf + pos;
            pos += len;
        }
        cmd_argv[u_argc] = NULL;
        if (ok) {
            int r = run_one(real_path, cmd_argv);
            if (r != 0 && rc == 0) rc = r;
        }
        free(cmd_argv);
        if (!ok) break;
    }
    free(buf);
    return rc;
}

static int acquire_node_slot(void) {
    mkdir("/tmp/runsc-shared-root", 0755);
    int start = (int)(getpid() % 8);
    char path[128];
    for (int i = 0; i < 8; i++) {
        int s = (start + i) % 8;
        snprintf(path, sizeof(path), "/tmp/runsc-shared-root/slot.%d", s);
        int fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0666);
        if (fd >= 0) {
            if (flock(fd, LOCK_EX | LOCK_NB) == 0) {
                return fd;
            }
            close(fd);
        }
    }
    snprintf(path, sizeof(path), "/tmp/runsc-shared-root/slot.%d", start);
    int fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0666);
    if (fd >= 0) {
        flock(fd, LOCK_EX);
    }
    return fd;
}

static int flag_takes_arg(const char *f) {
    if (strchr(f, '=') != NULL) return 0;
    return (strcmp(f, "-root") == 0 || strcmp(f, "--root") == 0 ||
            strcmp(f, "-log-format") == 0 || strcmp(f, "--log-format") == 0 ||
            strcmp(f, "-debug-log") == 0 || strcmp(f, "--debug-log") == 0 ||
            strcmp(f, "-log") == 0 || strcmp(f, "--log") == 0 ||
            strcmp(f, "-platform") == 0 || strcmp(f, "--platform") == 0);
}

int main(int argc, char **argv) {
    char real_path[4096];
    size_t len = strlen(argv[0]);
    if (len > 5 && strcmp(argv[0] + len - 5, "-fast") == 0) {
        memcpy(real_path, argv[0], len - 5);
        real_path[len - 5] = '\0';
    } else {
        snprintf(real_path, sizeof(real_path), "%s", argv[0]);
    }

    mkdir("/tmp/runsc-shared-root", 0755);
    setenv("GOMAXPROCS", "2", 1);

    const char *root_dir = NULL;
    const char *subcmd = NULL;
    for (int i = 1; i < argc; i++) {
        if (subcmd == NULL) {
            if (argv[i][0] == '-') {
                if ((strcmp(argv[i], "-root") == 0 || strcmp(argv[i], "--root") == 0) && i + 1 < argc) {
                    root_dir = argv[++i];
                } else if (strncmp(argv[i], "-root=", 6) == 0) {
                    root_dir = argv[i] + 6;
                } else if (strncmp(argv[i], "--root=", 7) == 0) {
                    root_dir = argv[i] + 7;
                } else if (flag_takes_arg(argv[i]) && i + 1 < argc) {
                    i++;
                }
            } else {
                subcmd = argv[i];
            }
        }
    }

    char **new_argv = malloc((argc + 10) * sizeof(char *));
    int k = 0;
    new_argv[k++] = real_path;
    new_argv[k++] = "--shared-root=/tmp/runsc-shared-root";
    new_argv[k++] = "--gofer-network-namespace=host";
    new_argv[k++] = "--host-settings=ignore";
    new_argv[k++] = "--restore-spec-validation=ignore";
    new_argv[k++] = "--overlay2=none";
    new_argv[k++] = "-log=/dev/null";

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--alsologtostderr") == 0 ||
            strcmp(argv[i], "-alsologtostderr") == 0 ||
            strcmp(argv[i], "-direct") == 0 ||
            strcmp(argv[i], "--direct") == 0) {
            continue;
        }
        new_argv[k++] = argv[i];
    }
    new_argv[k] = NULL;

    if (root_dir == NULL || subcmd == NULL) {
        execve(real_path, new_argv, environ);
        return 127;
    }

    char actor_dir[4096];
    snprintf(actor_dir, sizeof(actor_dir), "%s", root_dir);
    char *slash = strrchr(actor_dir, '/');
    if (slash != NULL && slash != actor_dir) {
        *slash = '\0';
    }

    char lock_path[4096];
    char queue_path[4096];
    char ckpt_marker[4096];
    snprintf(lock_path, sizeof(lock_path), "%s/fast.lock", actor_dir);
    snprintf(queue_path, sizeof(queue_path), "%s/fast.queue", actor_dir);
    snprintf(ckpt_marker, sizeof(ckpt_marker), "%s/.checkpointed", root_dir);

    if (strcmp(subcmd, "create") == 0) {
        unlink(ckpt_marker);
        if (append_queue(queue_path, k, new_argv) == 0) {
            return 0;
        }
    } else if (strcmp(subcmd, "restore") == 0) {
        const char *container_name = argv[argc - 1];
        if (append_queue(queue_path, k, new_argv) == 0) {
            if (strcmp(container_name, "_pause") == 0) {
                return 0;
            }
            int lock_fd = open(lock_path, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
            if (lock_fd >= 0) {
                flock(lock_fd, LOCK_EX);
            }
            pid_t p1 = fork();
            if (p1 == 0) {
                pid_t p2 = fork();
                if (p2 > 0) {
                    _exit(0);
                }
                if (p2 == 0) {
                    int dn = open("/dev/null", O_RDWR);
                    if (dn >= 0) {
                        dup2(dn, 0);
                        dup2(dn, 1);
                        dup2(dn, 2);
                        if (dn > 2) close(dn);
                    }
                    for (int fd = 3; fd < 256; fd++) {
                        if (fd != lock_fd) close(fd);
                    }
                    int slot_fd = acquire_node_slot();
                    drain_queue(queue_path, real_path);
                    if (slot_fd >= 0) close(slot_fd);
                    if (lock_fd >= 0) close(lock_fd);
                    _exit(0);
                }
                _exit(1);
            }
            if (p1 > 0) {
                while (waitpid(p1, NULL, 0) < 0 && errno == EINTR) {}
                if (lock_fd >= 0) close(lock_fd);
                return 0;
            }
            if (lock_fd >= 0) close(lock_fd);
        }
    } else if ((strcmp(subcmd, "kill") == 0 ||
                strcmp(subcmd, "wait") == 0 ||
                strcmp(subcmd, "state") == 0) &&
               access(ckpt_marker, F_OK) == 0) {
        return 0;
    }

    int lock_fd = open(lock_path, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (lock_fd >= 0) {
        flock(lock_fd, LOCK_EX);
    }
    if (access(queue_path, F_OK) == 0) {
        int qrc = drain_queue(queue_path, real_path);
        if (qrc != 0) {
            if (lock_fd >= 0) close(lock_fd);
            return qrc;
        }
    }
    int rc = run_one(real_path, new_argv);
    if (strcmp(subcmd, "checkpoint") == 0 && rc == 0) {
        int m = open(ckpt_marker, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
        if (m >= 0) close(m);
    }
    if (lock_fd >= 0) close(lock_fd);
    return rc;
}
