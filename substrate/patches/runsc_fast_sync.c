#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

extern char **environ;

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

    char **new_argv = malloc((argc + 10) * sizeof(char *));
    int k = 0;
    new_argv[k++] = real_path;
    new_argv[k++] = "--shared-root=/tmp/runsc-shared-root";
    new_argv[k++] = "--gofer-network-namespace=host";
    new_argv[k++] = "--host-settings=ignore";
    new_argv[k++] = "--restore-spec-validation=ignore";
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

    execve(real_path, new_argv, environ);
    perror("execve real runsc");
    return 127;
}
